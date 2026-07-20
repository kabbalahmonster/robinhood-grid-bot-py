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
    """Save wallet to file with restricted permissions. Appends if file exists."""
    # Ensure directory exists
    os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else '.', exist_ok=True)
    
    file_exists = os.path.exists(filepath)
    
    content = f"""
# {'='*60}
# Ethereum Wallet #{get_wallet_count(filepath) + 1} - Generated {wallet['created_at']}
# {'='*60}
# WARNING: Keep this file secure. Never share the private key.
# Anyone with the private key has full control of this wallet.

Address:    {wallet['address']}
PrivateKey: {wallet['private_key']}

# To use with the trading bot, set in your .env file:
# PRIVATE_KEY={wallet['private_key']}
# Or use this wallet address to receive funds.
"""
    
    # Append to file if it exists, create if it doesn't
    mode = 'a' if file_exists else 'w'
    with open(filepath, mode) as f:
        f.write(content)
    
    # Set restrictive permissions (owner read/write only) only on new files
    if chmod and not file_exists:
        os.chmod(filepath, 0o600)
    
    return filepath


def get_wallet_count(filepath: str) -> int:
    """Count existing wallets in the file by counting 'Ethereum Wallet #' headers."""
    if not os.path.exists(filepath):
        return 0
    try:
        with open(filepath, 'r') as f:
            content = f.read()
            return content.count('Ethereum Wallet #')
    except:
        return 0


def main():
    parser = argparse.ArgumentParser(
        description='Generate a new Ethereum wallet for trading',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate wallet and save to file (appends if file exists)
  python generate_wallet.py --output my_wallet.txt
  
  # Generate and create a new file (never append)
  python generate_wallet.py --output my_wallet.txt --new-file
  
  # Generate without saving (output to console only)
  python generate_wallet.py --no-save
  
  # Generate with custom permissions
  python generate_wallet.py --output wallet.txt --no-chmod

Security Notes:
  - Private keys are generated using Python's secrets module (cryptographically secure)
  - Output files are created with 600 permissions (owner read/write only)
  - Never commit wallet files to git or share private keys
  - Always verify the address matches the private key before sending funds
  - Multiple wallets in one file are numbered for easy reference
        """
    )
    parser.add_argument('--output', '-o', type=str, default='wallet.txt',
                        help='Output file path (default: wallet.txt)')
    parser.add_argument('--no-save', action='store_true',
                        help='Print to console only, do not save to file')
    parser.add_argument('--no-chmod', action='store_true',
                        help='Do not set restrictive file permissions')
    parser.add_argument('--new-file', '-n', action='store_true',
                        help='Create a new file instead of appending to existing')
    
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
    
    # Handle existing file - append instead of overwrite
    file_exists = os.path.exists(args.output)
    if file_exists and args.new_file:
        # Find a unique filename
        base, ext = os.path.splitext(args.output)
        counter = 1
        new_filepath = f"{base}_{counter}{ext}"
        while os.path.exists(new_filepath):
            counter += 1
            new_filepath = f"{base}_{counter}{ext}"
        args.output = new_filepath
        print(f"📁 Creating new file: {args.output}")
    elif file_exists:
        wallet_num = get_wallet_count(args.output) + 1
        print(f"📁 Appending to existing file: {args.output} (Wallet #{wallet_num})")
    else:
        print(f"📁 Creating new file: {args.output}")
    
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
