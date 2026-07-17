#!/usr/bin/env python3
"""
Generate a new Ethereum wallet for trading.
Outputs address and private key to a file.

WARNING: Store the generated file securely. Anyone with the private key
has full control of the wallet and all funds.
"""

import os
import sys
import argparse
from datetime import datetime
from eth_account import Account
import secrets


def generate_wallet():
    """Generate a new Ethereum wallet with cryptographically secure random key."""
    # Generate 32 bytes of cryptographically secure randomness
    private_key_bytes = secrets.token_bytes(32)
    
    # Create account from private key
    account = Account.from_key(private_key_bytes)
    
    return {
        'address': account.address,
        'private_key': account.key.hex(),
        'created_at': datetime.utcnow().isoformat() + 'Z'
    }


def save_wallet(wallet: dict, filepath: str, chmod: bool = True):
    """Save wallet to file with restricted permissions."""
    # Ensure directory exists
    os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else '.', exist_ok=True)
    
    content = f"""# Ethereum Wallet - Generated {wallet['created_at']}
# WARNING: Keep this file secure. Never share the private key.
# Anyone with the private key has full control of this wallet.

Address:    {wallet['address']}
PrivateKey: {wallet['private_key']}

# To use with the trading bot, set in your .env file:
# PRIVATE_KEY={wallet['private_key']}
# Or use this wallet address to receive funds.
"""
    
    # Check if file already exists
    if os.path.exists(filepath):
        raise FileExistsError(f"File already exists: {filepath}")
    
    # Write file
    with open(filepath, 'w') as f:
        f.write(content)
    
    # Set restrictive permissions (owner read/write only)
    if chmod:
        os.chmod(filepath, 0o600)
    
    return filepath


def main():
    parser = argparse.ArgumentParser(
        description='Generate a new Ethereum wallet for trading',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate wallet and save to file
  python generate_wallet.py --output my_wallet.txt
  
  # Generate without saving (output to console only)
  python generate_wallet.py --no-save
  
  # Generate with custom permissions
  python generate_wallet.py --output wallet.txt --no-chmod

Security Notes:
  - Private keys are generated using Python's secrets module (cryptographically secure)
  - Output files are created with 600 permissions (owner read/write only)
  - Never commit wallet files to git or share private keys
  - Always verify the address matches the private key before sending funds
        """
    )
    parser.add_argument('--output', '-o', type=str, default='wallet.txt',
                        help='Output file path (default: wallet.txt)')
    parser.add_argument('--no-save', action='store_true',
                        help='Print to console only, do not save to file')
    parser.add_argument('--no-chmod', action='store_true',
                        help='Do not set restrictive file permissions')
    parser.add_argument('--force', '-f', action='store_true',
                        help='Overwrite existing file if it exists')
    
    args = parser.parse_args()
    
    print("="*70)
    print("ETHEREUM WALLET GENERATOR")
    print("="*70)
    print()
    
    # Generate wallet
    print("🔐 Generating cryptographically secure wallet...")
    wallet = generate_wallet()
    print("✅ Wallet generated successfully")
    print()
    
    # Display wallet info
    print("📋 Wallet Details:")
    print("-"*70)
    print(f"Address:    {wallet['address']}")
    print(f"PrivateKey: {wallet['private_key']}")
    print(f"Created:    {wallet['created_at']}")
    print("-"*70)
    print()
    
    # Security warning
    print("⚠️  SECURITY WARNINGS:")
    print("   1. NEVER share your private key with anyone")
    print("   2. NEVER commit this wallet to a public git repository")
    print("   3. ALWAYS verify the address before sending funds")
    print("   4. STORE this file in a secure location (encrypted drive, hardware wallet backup)")
    print("   5. Anyone with the private key has FULL CONTROL of this wallet")
    print()
    
    if args.no_save:
        print("💾 Output to console only (not saved to file)")
        print("   Copy the address and private key above to your .env file")
        return
    
    # Handle existing file
    if os.path.exists(args.output) and not args.force:
        print(f"❌ File already exists: {args.output}")
        print("   Use --force to overwrite or choose a different filename")
        sys.exit(1)
    
    # Save wallet
    try:
        filepath = save_wallet(wallet, args.output, chmod=not args.no_chmod)
        print(f"💾 Wallet saved to: {filepath}")
        
        if not args.no_chmod:
            print("   Permissions set to 600 (owner read/write only)")
        
        print()
        print("📖 Next Steps:")
        print(f"   1. Fund the wallet by sending ETH to: {wallet['address']}")
        print(f"   2. Add the private key to your .env file:")
        print(f"      PRIVATE_KEY={wallet['private_key']}")
        print(f"   3. Keep {filepath} as a secure backup")
        print()
        
    except Exception as e:
        print(f"❌ Error saving wallet: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
