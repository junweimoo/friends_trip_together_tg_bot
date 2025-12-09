import os
from datetime import datetime
from sqlalchemy import (
    select, delete, func, 
    Column, BigInteger, String, DateTime, Numeric, Integer, ForeignKey, Index
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

class PayRecord(Base):
    __tablename__ = 'pay_records'
    pay_record_id = Column(Integer, primary_key=True, autoincrement=True)
    gmt_created = Column(DateTime, default=datetime.utcnow)
    gmt_modified = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    chat_id = Column(BigInteger, nullable=False)
    thread_id = Column(Integer, nullable=True)
    from_user_id = Column(BigInteger, nullable=False)
    to_user_id = Column(BigInteger, nullable=False)
    currency = Column(String(10), nullable=False)
    value = Column(Numeric(10, 2), nullable=False)
    __table_args__ = (
        Index('idx_pay_context', 'chat_id', 'thread_id'),
        Index('idx_pay_from', 'from_user_id'),
        Index('idx_pay_to', 'to_user_id'),
    )

class PaymentGroup(Base):
    __tablename__ = 'payment_groups'
    group_id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(BigInteger, nullable=False)
    thread_id = Column(Integer, nullable=True)
    name = Column(String(255), nullable=False)
    gmt_created = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index('idx_group_context', 'chat_id', 'thread_id'),
    )

class PaymentGroupLink(Base):
    __tablename__ = 'payment_group_links'
    link_id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey('payment_groups.group_id'), nullable=False)
    pay_record_id = Column(Integer, ForeignKey('pay_records.pay_record_id'), nullable=False)

class User(Base):
    __tablename__ = 'users'
    user_id = Column(BigInteger, primary_key=True)
    chat_id = Column(BigInteger, primary_key=True)
    thread_id = Column(Integer, primary_key=True)
    gmt_created = Column(DateTime, default=datetime.utcnow)
    gmt_modified = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    name = Column(String(255))
    __table_args__ = (
        Index('idx_user_context', 'chat_id', 'thread_id'),
    )

async_session_factory = None

async def init_db(db_url):
    global async_session_factory
    engine = create_async_engine(db_url, echo=False) 
    
    async_session_factory = sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    print("Database initialized.")

def get_session():
    if async_session_factory is None:
        raise Exception("Database not initialized. Call init_db first.")
    return async_session_factory()

### USERS ###

async def get_chat_users(session, chat_id, thread_id):
    """
    Fetches all registered users for a specific chat context.
    """
    safe_thread_id = thread_id if thread_id is not None else 0
    stmt = select(User).where(
        User.chat_id == chat_id, 
        User.thread_id == safe_thread_id
    )
    result = await session.execute(stmt)
    return result.scalars().all()

async def upsert_user(user_id, chat_id, thread_id, username):
    """
    Inserts a new user or updates an existing one.
    """
    safe_thread_id = thread_id if thread_id is not None else 0
    async with get_session() as session:
        new_user = User(
            user_id=user_id,
            chat_id=chat_id,
            thread_id=safe_thread_id,
            name=username
        )
        await session.merge(new_user)
        await session.commit()

async def check_username_exists(chat_id, thread_id, username):
    """
    Checks if a username is already taken in the specific chat/thread.
    """
    safe_thread_id = thread_id if thread_id is not None else 0
    async with get_session() as session:
        stmt = select(User.user_id).where(
            User.chat_id == chat_id, 
            User.thread_id == safe_thread_id,
            func.lower(User.name) == func.lower(username)
        ).limit(1)
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None

### PAYMENT_RECORDS ###

async def create_payment(chat_id, thread_id, payer_id, payee_id, currency, amount):
    """
    Creates a payment record and returns the payee's name for display.
    """
    async with get_session() as session:
        record = PayRecord(
            chat_id=chat_id,
            thread_id=thread_id,
            from_user_id=payer_id,
            to_user_id=payee_id,
            currency=currency,
            value=amount
        )
        session.add(record)
        
        stmt = select(User.name).where(User.user_id == payee_id)
        result = await session.execute(stmt)
        payee_name = result.scalar_one_or_none() or "Unknown"

        await session.commit()
        
        return payee_name

async def create_full_transaction(chat_id, thread_id, payer_id, payee_id_or_split, currency, total_amount, description):
    async with get_session() as session:
        # 1. Create the Group
        group = PaymentGroup(
            chat_id=chat_id,
            thread_id=thread_id,
            name=description
        )
        session.add(group)
        await session.flush() # Flush to get group_id

        created_records = []

        # 2. Determine Logic: Split by amount, Split equally, or Single payee
        if isinstance(payee_id_or_split, dict) and payee_id_or_split.get('type') == 'DETAILED_SPLIT':
            # --- SPLIT BY AMOUNTS LOGIC ---
            allocations = payee_id_or_split['allocations']
            for payee_id, payee_amount in allocations.items():
                if payee_id == payer_id:
                    continue
                record = PayRecord(
                    chat_id=chat_id,
                    thread_id=thread_id,
                    from_user_id=payer_id,
                    to_user_id=payee_id,
                    currency=currency,
                    value=payee_amount
                )
                session.add(record)
                created_records.append(record)
            pass

        elif payee_id_or_split == "SPLIT_ALL":
            # --- SPLIT EQUALLY LOGIC ---
            all_users = await get_chat_users(session, chat_id, thread_id)
            
            if not all_users:
                raise Exception("No users found to split.")

            # Calculate Split Amount
            # Formula: Total / Count.
            # Payer creates debt records only against others.
            count = len(all_users)
            split_amount = total_amount / count

            for user in all_users:
                # Don't create a debt record for the payer paying themselves
                if user.user_id == payer_id:
                    continue
                
                record = PayRecord(
                    chat_id=chat_id,
                    thread_id=thread_id,
                    from_user_id=payer_id,
                    to_user_id=user.user_id,
                    currency=currency,
                    value=split_amount
                )
                session.add(record)
                created_records.append(record)
                
        else:
            # --- SINGLE PAYEE LOGIC ---
            payee_id = int(payee_id_or_split)
            record = PayRecord(
                chat_id=chat_id,
                thread_id=thread_id,
                from_user_id=payer_id,
                to_user_id=payee_id,
                currency=currency,
                value=total_amount
            )
            session.add(record)
            created_records.append(record)

        await session.flush() # Flush to get record IDs

        # 3. Link Records to Group
        for rec in created_records:
            link = PaymentGroupLink(
                group_id=group.group_id,
                pay_record_id=rec.pay_record_id
            )
            session.add(link)

        await session.commit()
        return len(created_records)

async def delete_last_transaction(user_id, chat_id, thread_id):
    async with get_session() as session:
        # 1. Find the most recently created PaymentGroup ID in this context
        stmt_find_group = select(PaymentGroup.group_id).where(
            PaymentGroup.chat_id == chat_id,
            PaymentGroup.thread_id == thread_id,
        ).order_by(PaymentGroup.gmt_created.desc()).limit(1)
        
        group_id_to_delete = (await session.execute(stmt_find_group)).scalar_one_or_none()

        if not group_id_to_delete:
            return False # No group found

        # 2. Find ALL PayRecord IDs belonging to that group
        stmt_find_all_records = select(PaymentGroupLink.pay_record_id).where(
            PaymentGroupLink.group_id == group_id_to_delete
        )
        record_ids_in_group = (await session.execute(stmt_find_all_records)).scalars().all()
        
        # 3. Delete all links in the group
        stmt_delete_links = delete(PaymentGroupLink).where(
            PaymentGroupLink.group_id == group_id_to_delete
        )
        await session.execute(stmt_delete_links)

        # 4. Delete all PayRecords that belonged to the group (using .in_() for the list)
        if record_ids_in_group:
            stmt_delete_records = delete(PayRecord).where(
                PayRecord.pay_record_id.in_(record_ids_in_group)
            )
            await session.execute(stmt_delete_records)

        # 5. Delete the PaymentGroup itself
        stmt_delete_group = delete(PaymentGroup).where(
            PaymentGroup.group_id == group_id_to_delete
        )
        await session.execute(stmt_delete_group)

        await session.commit()
        return True