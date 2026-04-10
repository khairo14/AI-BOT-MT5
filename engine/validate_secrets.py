"""
Secrets Validation — Task #6: Secrets Management
Ensures all required environment variables are set before starting.
Run at application startup to catch configuration errors early.
"""

import os
import sys
from pathlib import Path
from typing import List, Tuple

from dotenv import load_dotenv
from loguru import logger

# Load .env file if it exists
load_dotenv()


def validate_secrets() -> Tuple[bool, List[str]]:
    """
    Validate that all required environment variables are set.
    
    Returns:
        (is_valid, missing_vars): Tuple of validation status and list of missing variables
    """
    required_vars = [
        "MT5_DEMO_LOGIN",
        "MT5_DEMO_PASSWORD",
        "MT5_DEMO_SERVER",
        "MT5_LIVE_LOGIN",
        "MT5_LIVE_PASSWORD",
        "MT5_LIVE_SERVER",
    ]
    
    optional_vars = [
        "API_SECRET_KEY",  # Important for multi-user, but not required for single-user
        "API_HOST",
        "API_PORT",
        "DATABASE_URL",
    ]
    
    missing = []
    for var in required_vars:
        value = os.getenv(var)
        if not value or value.strip() == "":
            missing.append(var)
    
    # Check optional vars and warn if missing
    for var in optional_vars:
        value = os.getenv(var)
        if not value or value.strip() == "":
            logger.warning(f"Optional environment variable '{var}' not set")
    
    return (len(missing) == 0, missing)


def check_env_file_exists() -> bool:
    """Check if .env file exists in project root."""
    env_path = Path(".env")
    return env_path.exists()


def main():
    """Run validation checks and report results."""
    logger.info("🔒 Validating secrets management configuration...")
    
    # Check 1: .env file exists
    if not check_env_file_exists():
        logger.error(
            "❌ .env file not found!\n"
            "   Copy .env.example to .env and fill in your credentials:\n"
            "   > Copy-Item .env.example .env\n"
            "   > notepad .env"
        )
        return False
    
    logger.success("✅ .env file exists")
    
    # Check 2: Required variables are set
    is_valid, missing = validate_secrets()
    
    if not is_valid:
        logger.error(
            f"❌ Missing required environment variables: {', '.join(missing)}\n"
            "   Edit your .env file and add these values."
        )
        return False
    
    logger.success("✅ All required environment variables are set")
    
    # Check 3: Verify .gitignore
    gitignore_path = Path(".gitignore")
    if gitignore_path.exists():
        gitignore_content = gitignore_path.read_text()
        if ".env" in gitignore_content:
            logger.success("✅ .env file is excluded from git (secure)")
        else:
            logger.warning(
                "⚠️  .env file may not be excluded from git!\n"
                "   Add '.env' to .gitignore to prevent credential leakage."
            )
    
    logger.success("🎉 Secrets management validation complete!")
    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
