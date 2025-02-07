from flask import Flask, jsonify
from flask_cors import CORS
from solana.rpc.api import Client

app = Flask(__name__)
CORS(app)
client = Client("https://api.mainnet-beta.solana.com")

@app.route('/transactions')
def get_transactions():
    try:
        # Fetch recent transactions
        recent_transactions = client.get_recent_transactions()
        results = []

        # Process transactions
        for tx in recent_transactions.value:
            tx_signature = tx.transaction.signatures[0]
            tx_details = client.get_transaction(tx_signature)

            # Extract transaction amount
            pre_balance = tx_details.value.transaction.meta.pre_balances[0]
            post_balance = tx_details.value.transaction.meta.post_balances[0]
            amount = post_balance - pre_balance

            # Filter transactions over $10,000 (in lamports, 1 SOL = 1,000,000,000 lamports)
            if amount > 10000000000:  # 10 SOL = $10,000 (approx)
                wallet = tx_details.value.transaction.transaction.message.account_keys[0]
                results.append({
                    'wallet': str(wallet),
                    'amount': amount / 1_000_000_000,  # Convert lamports to SOL
                    'token': 'SOL'  # Default to SOL for now
                })

        return jsonify(results)

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run()
