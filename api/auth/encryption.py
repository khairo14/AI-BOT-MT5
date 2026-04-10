"""
AES-256 encryption/decryption for sensitive data (MT5 passwords).
"""

import base64
import os

from cryptography.fernet import Fernet
from loguru import logger

# Load encryption key from environment (loaded by main.py dotenv)
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")

if not ENCRYPTION_KEY:
    raise ValueError("ENCRYPTION_KEY not found in environment variables")

# Initialize Fernet cipher with the encryption key
fernet = Fernet(ENCRYPTION_KEY.encode())


def encrypt_password(plaintext_password: str) -> str:
    """
    Encrypt a plaintext password using AES-256.
    
    Args:
        plaintext_password: The password to encrypt
        
    Returns:
        Base64-encoded encrypted password string
        
    Example:
        >>> encrypted = encrypt_password("MyMT5Password123")
        >>> print(encrypted)
        'gAAAAABk...'
    """
    try:
        encrypted_bytes = fernet.encrypt(plaintext_password.encode())
        return encrypted_bytes.decode()
    except Exception as e:
        logger.error(f"Password encryption failed: {e}")
        raise ValueError("Failed to encrypt password")


def decrypt_password(encrypted_password: str) -> str:
    """
    Decrypt an encrypted password.
    
    Args:
        encrypted_password: Base64-encoded encrypted password
        
    Returns:
        Decrypted plaintext password
        
    Example:
        >>> plaintext = decrypt_password("gAAAAABk...")
        >>> print(plaintext)
        'MyMT5Password123'
    """
    try:
        decrypted_bytes = fernet.decrypt(encrypted_password.encode())
        return decrypted_bytes.decode()
    except Exception as e:
        logger.error(f"Password decryption failed: {e}")
        raise ValueError("Failed to decrypt password - data may be corrupted")


def test_encryption() -> bool:
    """
    Test encryption/decryption roundtrip.
    
    Returns:
        True if encryption works correctly
    """
    test_password = "TestMT5Password123!"
    
    try:
        encrypted = encrypt_password(test_password)
        decrypted = decrypt_password(encrypted)
        
        if decrypted == test_password:
            logger.info("✅ Encryption test passed")
            return True
        else:
            logger.error("❌ Encryption test failed: passwords don't match")
            return False
    except Exception as e:
        logger.error(f"❌ Encryption test failed: {e}")
        return False


if __name__ == "__main__":
    # Run test when executed directly
    from dotenv import load_dotenv
    load_dotenv()
    test_encryption()
