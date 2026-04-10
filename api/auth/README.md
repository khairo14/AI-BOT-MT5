# User Authentication System

## Overview

JWT-based authentication with:
- ✅ User registration with password strength validation
- ✅ Login with bcrypt password hashing
- ✅ Access tokens (1 hour expiration)
- ✅ Refresh tokens (7 days expiration)
- ✅ Role-based access control (admin/user/readonly)
- ✅ Audit logging for all auth events

---

## API Endpoints

### **POST /auth/register**
Register a new user account.

**Request:**
```json
{
  "email": "user@example.com",
  "password": "SecurePass123"
}
```

**Password Requirements:**
- Minimum 8 characters
- At least 1 uppercase letter
- At least 1 lowercase letter
- At least 1 digit

**Response (201 Created):**
```json
{
  "id": "uuid",
  "email": "user@example.com",
  "role": "user",
  "is_active": true,
  "email_verified": false,
  "created_at": "2026-04-11T10:30:00Z",
  "last_login_at": null
}
```

---

### **POST /auth/login**
Login and receive JWT tokens.

**Request:**
```json
{
  "email": "user@example.com",
  "password": "SecurePass123"
}
```

**Response (200 OK):**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 3600
}
```

---

### **POST /auth/refresh**
Refresh access token using refresh token.

**Request:**
```json
{
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
}
```

**Response (200 OK):**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 3600
}
```

---

### **GET /auth/me**
Get current user profile (requires authentication).

**Headers:**
```
Authorization: Bearer <access_token>
```

**Response (200 OK):**
```json
{
  "id": "uuid",
  "email": "user@example.com",
  "role": "user",
  "is_active": true,
  "email_verified": false,
  "created_at": "2026-04-11T10:30:00Z",
  "last_login_at": "2026-04-11T11:00:00Z"
}
```

---

### **POST /auth/logout**
Logout current user (requires authentication).

**Headers:**
```
Authorization: Bearer <access_token>
```

**Response (200 OK):**
```json
{
  "status": "ok",
  "message": "Logged out successfully"
}
```

**Note:** Client must delete tokens from storage. Server-side logout logs the event for audit trail.

---

## Protected Routes

### Using Authentication Dependency

```python
from fastapi import APIRouter, Depends
from api.auth.dependencies import get_current_user
from database.models import User

router = APIRouter()

@router.get("/protected")
def protected_route(user: User = Depends(get_current_user)):
    return {"message": f"Hello {user.email}!"}
```

### Admin-Only Routes

```python
from api.auth.dependencies import require_admin

@router.delete("/admin/users/{user_id}")
def delete_user(
    user_id: str,
    admin: User = Depends(require_admin)
):
    # Only admins can access this route
    return {"status": "deleted"}
```

### Optional Authentication

```python
from api.auth.dependencies import get_current_user_optional
from typing import Optional

@router.get("/signals")
def get_signals(user: Optional[User] = Depends(get_current_user_optional)):
    if user:
        # Show user-specific signals
        return get_user_signals(user.id)
    else:
        # Show public signals
        return get_public_signals()
```

---

## Client-Side Usage

### 1. Register New Account

```javascript
const response = await fetch('http://localhost:8000/auth/register', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    email: 'trader@example.com',
    password: 'SecurePass123'
  })
});

const user = await response.json();
console.log('Registered:', user.email);
```

### 2. Login

```javascript
const response = await fetch('http://localhost:8000/auth/login', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    email: 'trader@example.com',
    password: 'SecurePass123'
  })
});

const { access_token, refresh_token } = await response.json();

// Store tokens (localStorage, sessionStorage, or secure cookie)
localStorage.setItem('access_token', access_token);
localStorage.setItem('refresh_token', refresh_token);
```

### 3. Make Authenticated Requests

```javascript
const accessToken = localStorage.getItem('access_token');

const response = await fetch('http://localhost:8000/trades/positions', {
  headers: {
    'Authorization': `Bearer ${accessToken}`
  }
});

const positions = await response.json();
```

### 4. Handle Token Expiration

```javascript
async function fetchWithAuth(url, options = {}) {
  let accessToken = localStorage.getItem('access_token');
  
  // Try request with current token
  let response = await fetch(url, {
    ...options,
    headers: {
      ...options.headers,
      'Authorization': `Bearer ${accessToken}`
    }
  });
  
  // If 401 Unauthorized, refresh token and retry
  if (response.status === 401) {
    const refreshToken = localStorage.getItem('refresh_token');
    
    const refreshResponse = await fetch('http://localhost:8000/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken })
    });
    
    if (refreshResponse.ok) {
      const { access_token, refresh_token } = await refreshResponse.json();
      localStorage.setItem('access_token', access_token);
      localStorage.setItem('refresh_token', refresh_token);
      
      // Retry original request with new token
      response = await fetch(url, {
        ...options,
        headers: {
          ...options.headers,
          'Authorization': `Bearer ${access_token}`
        }
      });
    } else {
      // Refresh failed, redirect to login
      window.location.href = '/login';
    }
  }
  
  return response;
}
```

### 5. Logout

```javascript
const accessToken = localStorage.getItem('access_token');

await fetch('http://localhost:8000/auth/logout', {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${accessToken}`
  }
});

// Clear tokens
localStorage.removeItem('access_token');
localStorage.removeItem('refresh_token');

// Redirect to login
window.location.href = '/login';
```

---

## Security Features

### Password Hashing
- **bcrypt** with automatic salt generation
- Minimum 8 characters
- Uppercase + lowercase + digit requirements

### JWT Tokens
- **HS256** algorithm (HMAC with SHA-256)
- Access tokens expire in **1 hour**
- Refresh tokens expire in **7 days**
- Token type validation (access vs refresh)

### Audit Logging
All authentication events logged to `audit_log` table:
- User registration
- Login success/failure
- Logout
- IP address and user agent tracking

### Role-Based Access Control
- **admin**: Full system access, can manage other users
- **user**: Normal user, access to own data only
- **readonly**: View-only access

---

## Testing with cURL

### Register
```bash
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"TestPass123"}'
```

### Login
```bash
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"TestPass123"}'
```

### Get Current User
```bash
curl http://localhost:8000/auth/me \
  -H "Authorization: Bearer <access_token>"
```

### Refresh Token
```bash
curl -X POST http://localhost:8000/auth/refresh \
  -H "Content-Type: application/json" \
  -d '{"refresh_token":"<refresh_token>"}'
```

---

## Next Steps (Task #19)

After authentication is working:
1. **MT5 Account Management**: Link MT5 accounts to user profiles
2. **Dashboard Login Page**: Create login/register UI
3. **Token Storage**: Implement secure token management in dashboard
4. **Protected Routes**: Add authentication to all user-specific endpoints
5. **User Settings**: Per-user risk settings, strategy selections

---

## Configuration

Environment variables in `.env`:

```env
# JWT Authentication
JWT_SECRET_KEY=<generated-secret-key>
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=60
REFRESH_TOKEN_EXPIRE_DAYS=7

# Database encryption (for MT5 passwords)
ENCRYPTION_KEY=<generated-encryption-key>
```

**Security Note:** Never commit `.env` to git! Keep JWT_SECRET_KEY and ENCRYPTION_KEY secret.
