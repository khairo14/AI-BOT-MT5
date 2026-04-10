"""
MT5 Account Management — CRUD endpoints with RBAC.

All endpoints require authentication and enforce user ownership.
Users can only access/manage their own MT5 accounts.
"""

from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import BaseModel, Field, validator
from sqlalchemy.orm import Session

from api.auth.dependencies import get_current_user, require_admin
from api.auth.encryption import encrypt_password, decrypt_password
from database.connection import get_db
from database.models import MT5Account, User, UserRole

router = APIRouter()


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------

class AddMT5AccountRequest(BaseModel):
    """Request to add a new MT5 account."""
    login: int = Field(..., ge=1, description="MT5 account login number")
    password: str = Field(..., min_length=1, max_length=100, description="MT5 account password")
    server: str = Field(..., min_length=1, max_length=200, description="MT5 server address")
    broker_name: Optional[str] = Field(None, max_length=100, description="Broker name for display")
    is_demo: bool = Field(False, description="Is this a demo account?")
    set_as_primary: bool = Field(False, description="Set as primary account after creation?")
    
    @validator("server")
    def normalize_server(cls, v):
        """Remove extra whitespace from server name."""
        return v.strip()


class UpdateMT5AccountRequest(BaseModel):
    """Request to update an existing MT5 account."""
    password: Optional[str] = Field(None, min_length=1, max_length=100)
    server: Optional[str] = Field(None, min_length=1, max_length=200)
    broker_name: Optional[str] = Field(None, max_length=100)
    is_demo: Optional[bool] = None


class MT5AccountResponse(BaseModel):
    """MT5 account details (password is never returned)."""
    id: str
    user_id: str
    login: int
    server: str
    broker_name: Optional[str]
    is_demo: bool
    is_primary: bool
    last_connected_at: Optional[str]
    created_at: str
    
    class Config:
        from_attributes = True


class ConnectionTestResponse(BaseModel):
    """Result of MT5 connection test."""
    success: bool
    message: str
    account_balance: Optional[float] = None
    account_equity: Optional[float] = None
    account_currency: Optional[str] = None


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def get_user_account(
    account_id: UUID,
    user: User,
    db: Session,
    allow_admin_access: bool = False
) -> MT5Account:
    """
    Get MT5 account by ID, enforcing ownership.
    
    Args:
        account_id: The account UUID
        user: The authenticated user
        db: Database session
        allow_admin_access: If True, admins can access any account
        
    Returns:
        The MT5Account if found and user has access
        
    Raises:
        HTTPException 404 if account not found or user doesn't own it
    """
    account = db.query(MT5Account).filter(MT5Account.id == account_id).first()
    
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MT5 account not found",
        )
    
    # Check ownership (or admin override)
    is_owner = account.user_id == user.id
    is_admin = user.role == UserRole.ADMIN and allow_admin_access
    
    if not (is_owner or is_admin):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,  # Return 404 to not leak existence
            detail="MT5 account not found",
        )
    
    return account


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/mt5-accounts", response_model=MT5AccountResponse, status_code=status.HTTP_201_CREATED)
def add_mt5_account(
    data: AddMT5AccountRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Add a new MT5 account to the authenticated user's profile.
    
    **RBAC**: Requires authentication. Users can only add accounts to their own profile.
    
    **Password Security**: MT5 password is encrypted with AES-256 before storage.
    
    **Primary Account**: If 'set_as_primary' is True or this is the user's first account,
    it will be set as the primary account for trading.
    
    **Returns**: The created MT5 account (password is NOT included in response).
    """
    # Check if account with this login already exists for this user
    existing = db.query(MT5Account).filter(
        MT5Account.user_id == user.id,
        MT5Account.login == data.login,
        MT5Account.server == data.server.strip(),
    ).first()
    
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"MT5 account {data.login} on {data.server} already exists",
        )
    
    # Encrypt password before storing
    encrypted_password = encrypt_password(data.password)
    
    # Check if this is user's first account (auto-set as primary)
    existing_accounts_count = db.query(MT5Account).filter(
        MT5Account.user_id == user.id
    ).count()
    
    is_primary = data.set_as_primary or (existing_accounts_count == 0)
    
    # If setting as primary, unset other accounts
    if is_primary:
        db.query(MT5Account).filter(
            MT5Account.user_id == user.id,
            MT5Account.is_primary == True
        ).update({"is_primary": False})
    
    # Create new account
    new_account = MT5Account(
        user_id=user.id,
        login=data.login,
        encrypted_password=encrypted_password,
        server=data.server.strip(),
        broker_name=data.broker_name,
        is_demo=data.is_demo,
        is_primary=is_primary,
    )
    
    db.add(new_account)
    db.commit()
    db.refresh(new_account)
    
    logger.info(f"User {user.email} added MT5 account {new_account.login} (primary={is_primary})")
    
    return MT5AccountResponse(
        id=str(new_account.id),
        user_id=str(new_account.user_id),
        login=new_account.login,
        server=new_account.server,
        broker_name=new_account.broker_name,
        is_demo=new_account.is_demo,
        is_primary=new_account.is_primary,
        last_connected_at=new_account.last_connected_at.isoformat() if new_account.last_connected_at else None,
        created_at=new_account.created_at.isoformat(),
    )


@router.get("/mt5-accounts", response_model=List[MT5AccountResponse])
def list_mt5_accounts(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    List all MT5 accounts for the authenticated user.
    
    **RBAC**: Requires authentication. Users can only see their own accounts.
    
    **Returns**: List of MT5 accounts (passwords are NOT included).
    """
    accounts = db.query(MT5Account).filter(
        MT5Account.user_id == user.id
    ).order_by(MT5Account.is_primary.desc(), MT5Account.created_at.desc()).all()
    
    return [
        MT5AccountResponse(
            id=str(acc.id),
            user_id=str(acc.user_id),
            login=acc.login,
            server=acc.server,
            broker_name=acc.broker_name,
            is_demo=acc.is_demo,
            is_primary=acc.is_primary,
            last_connected_at=acc.last_connected_at.isoformat() if acc.last_connected_at else None,
            created_at=acc.created_at.isoformat(),
        )
        for acc in accounts
    ]


@router.get("/mt5-accounts/{account_id}", response_model=MT5AccountResponse)
def get_mt5_account(
    account_id: UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Get details of a specific MT5 account.
    
    **RBAC**: Requires authentication and account ownership.
    
    **Returns**: MT5 account details (password is NOT included).
    """
    account = get_user_account(account_id, user, db)
    
    return MT5AccountResponse(
        id=str(account.id),
        user_id=str(account.user_id),
        login=account.login,
        server=account.server,
        broker_name=account.broker_name,
        is_demo=account.is_demo,
        is_primary=account.is_primary,
        last_connected_at=account.last_connected_at.isoformat() if account.last_connected_at else None,
        created_at=account.created_at.isoformat(),
    )


@router.patch("/mt5-accounts/{account_id}", response_model=MT5AccountResponse)
def update_mt5_account(
    account_id: UUID,
    data: UpdateMT5AccountRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Update an MT5 account's details.
    
    **RBAC**: Requires authentication and account ownership.
    
    **Updatable Fields**: password, server, broker_name, is_demo
    
    **Note**: To change primary account, use the /mt5-accounts/{id}/set-primary endpoint.
    """
    account = get_user_account(account_id, user, db)
    
    # Update fields
    if data.password is not None:
        account.encrypted_password = encrypt_password(data.password)
    
    if data.server is not None:
        account.server = data.server.strip()
    
    if data.broker_name is not None:
        account.broker_name = data.broker_name
    
    if data.is_demo is not None:
        account.is_demo = data.is_demo
    
    db.commit()
    db.refresh(account)
    
    logger.info(f"User {user.email} updated MT5 account {account.login}")
    
    return MT5AccountResponse(
        id=str(account.id),
        user_id=str(account.user_id),
        login=account.login,
        server=account.server,
        broker_name=account.broker_name,
        is_demo=account.is_demo,
        is_primary=account.is_primary,
        last_connected_at=account.last_connected_at.isoformat() if account.last_connected_at else None,
        created_at=account.created_at.isoformat(),
    )


@router.delete("/mt5-accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mt5_account(
    account_id: UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Delete an MT5 account.
    
    **RBAC**: Requires authentication and account ownership.
    
    **Warning**: This action cannot be undone. All related data will be deleted
    due to cascade delete constraints.
    
    **Primary Account**: If deleting the primary account, you must set another
    account as primary before deletion, or this will fail.
    """
    account = get_user_account(account_id, user, db)
    
    # Prevent deleting primary account if other accounts exist
    if account.is_primary:
        other_accounts = db.query(MT5Account).filter(
            MT5Account.user_id == user.id,
            MT5Account.id != account.id
        ).count()
        
        if other_accounts > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete primary account. Set another account as primary first.",
            )
    
    db.delete(account)
    db.commit()
    
    logger.info(f"User {user.email} deleted MT5 account {account.login}")
    
    return None


@router.patch("/mt5-accounts/{account_id}/set-primary", response_model=MT5AccountResponse)
def set_primary_account(
    account_id: UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Set an MT5 account as the primary account for trading.
    
    **RBAC**: Requires authentication and account ownership.
    
    **Effect**: The specified account becomes primary, and all other accounts
    for this user are set to non-primary.
    
    **Primary Account**: The primary account is used by default for all trading
    operations unless explicitly specified otherwise.
    """
    account = get_user_account(account_id, user, db)
    
    # Unset all other primary accounts for this user
    db.query(MT5Account).filter(
        MT5Account.user_id == user.id,
        MT5Account.id != account.id
    ).update({"is_primary": False})
    
    # Set this account as primary
    account.is_primary = True
    
    db.commit()
    db.refresh(account)
    
    logger.info(f"User {user.email} set MT5 account {account.login} as primary")
    
    return MT5AccountResponse(
        id=str(account.id),
        user_id=str(account.user_id),
        login=account.login,
        server=account.server,
        broker_name=account.broker_name,
        is_demo=account.is_demo,
        is_primary=account.is_primary,
        last_connected_at=account.last_connected_at.isoformat() if account.last_connected_at else None,
        created_at=account.created_at.isoformat(),
    )


@router.post("/mt5-accounts/{account_id}/test", response_model=ConnectionTestResponse)
def test_mt5_connection(
    account_id: UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Test connection to an MT5 account.
    
    **RBAC**: Requires authentication and account ownership.
    
    **Process**:
    1. Decrypt stored password
    2. Attempt connection to MT5 server
    3. Retrieve account balance and equity
    4. Update last_connected_at timestamp if successful
    
    **Returns**: Connection test result with account info if successful.
    """
    account = get_user_account(account_id, user, db)
    
    # Decrypt password
    try:
        plaintext_password = decrypt_password(account.encrypted_password)
    except Exception as e:
        logger.error(f"Failed to decrypt password for account {account.login}: {e}")
        return ConnectionTestResponse(
            success=False,
            message="Failed to decrypt account password. Please update your password.",
        )
    
    # Test MT5 connection
    try:
        import MetaTrader5 as mt5
        
        # Initialize MT5
        if not mt5.initialize():
            return ConnectionTestResponse(
                success=False,
                message=f"MT5 initialization failed: {mt5.last_error()}",
            )
        
        # Attempt login
        authorized = mt5.login(
            login=account.login,
            password=plaintext_password,
            server=account.server
        )
        
        if not authorized:
            mt5.shutdown()
            return ConnectionTestResponse(
                success=False,
                message=f"Login failed: {mt5.last_error()}. Check credentials.",
            )
        
        # Get account info
        account_info = mt5.account_info()
        
        if account_info is None:
            mt5.shutdown()
            return ConnectionTestResponse(
                success=False,
                message="Connected but failed to retrieve account info.",
            )
        
        # Update last_connected_at
        account.last_connected_at = datetime.now(timezone.utc)
        db.commit()
        
        # Shutdown MT5
        mt5.shutdown()
        
        logger.info(f"User {user.email} successfully tested MT5 account {account.login}")
        
        return ConnectionTestResponse(
            success=True,
            message="Connection successful!",
            account_balance=float(account_info.balance),
            account_equity=float(account_info.equity),
            account_currency=account_info.currency,
        )
        
    except Exception as e:
        logger.error(f"MT5 connection test failed for account {account.login}: {e}")
        return ConnectionTestResponse(
            success=False,
            message=f"Connection failed: {str(e)}",
        )
