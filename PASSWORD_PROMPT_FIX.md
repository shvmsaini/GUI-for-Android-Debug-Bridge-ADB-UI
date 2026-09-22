# Password Prompt Duplication Fix

## Issue
The password prompt was showing duplicated user information:
- **Before**: `"Enter password for user@user@pdc-ite79:"`  ❌
- **After**: `"Enter password for user@pdc-ite79:"` ✅

## Root Cause
The `CredentialManager.prompt_password()` method constructs the prompt as:
```python
f"Enter password for {user}@{gateway}:"
```

The method expects two separate parameters:
- `gateway`: hostname only (e.g., `"pdc-ite79"`)
- `user`: username only (e.g., `"user"`)

However, in two places in the code:

### 1. `connect_seat()` method (line ~3410-3418)
**Before** (incorrect):
```python
parts = gateway.split('@')
user = parts[0] if len(parts) == 2 else 'user'

password = self.credential_manager.get_password(gateway, user)  # ← passing full "user@pdc-ite79"
if not password:
    password = self.credential_manager.prompt_password(self, gateway, user)  # ← creates "user@user@pdc-ite79"
```

### 2. `connect_portforward()` method (line ~3545-3555)
**Before** (incorrect):
```python
if '@' in gateway:
    gateway_host = gateway.split('@', 1)[1]  # Correctly extracts just hostname
else:
    gateway_host = gateway

password = self.credential_manager.get_password(gateway, user)  # ← passing full gateway string instead of gateway_host!
```

## Solution
Fixed both methods to consistently pass the hostname-only portion to credential manager methods:

### 1. Updated `connect_seat()` method
```python
# Parse user from gateway
parts = gateway.split('@')
user = parts[0] if len(parts) == 2 else 'user'
# Get just the hostname part for credential manager
gateway_host = parts[1] if len(parts) == 2 else gateway

# Get password from Keychain only (no plain text storage)
password = self.credential_manager.get_password(gateway_host, user)  # ← now hostname only
if not password:
    password = self.credential_manager.prompt_password(self, gateway_host, user)  # ← now hostname only
```

### 2. Updated `connect_portforward()` method
```python
# Strip any user@ prefix from the gateway so the script sees a bare hostname
if '@' in gateway:
    gateway_host = gateway.split('@', 1)[1]
else:
    gateway_host = gateway

# Get password from Keychain only (no plain text storage)
# Keychain is keyed by the hostname and user, constructed as user@hostname
password = self.credential_manager.get_password(gateway_host, user)  # ← now hostname only (was: gateway)
if not password:
    password = self.credential_manager.prompt_password(self, gateway_host, user)  # ← already correct
```

## Affected Methods
All three credential manager methods now receive consistent parameters:
- `get_password(gateway_host, user)` → looks up `user@gateway_host` in keychain
- `set_password(gateway_host, user, password)` → stores `user@gateway_host` in keychain
- `prompt_password(self, gateway_host, user)` → displays `"Enter password for user@gateway_host:"`

## Testing
The fix ensures that:
1. The password prompt shows the correct format: `user@hostname` (not `user@user@hostname`)
2. Password storage and retrieval are consistent across seat and portforward connections
3. Keychain lookups work correctly when retrieving previously saved passwords
