from flask import Flask, jsonify
from flask_cors import CORS
from solana.rpc.api import Client

app = Flask(__name__)
CORS(app)
client = Client("https://api.mainnet-beta.solana.com")

@app.route('/transactions')
def get_transactions():
    transactions = client.get_recent_transactions()
    results = []
    for tx in transactions[:10]:  # Check first 10 transactions
        try:
            details = client.get_transaction(tx['signature'])
            amount = details['result']['meta']['postBalances'][0] - details['result']['meta']['preBalances'][0]
            if amount > 10000:
                results.append({
                    'wallet': details['result']['transaction']['message']['accountKeys'][0],
                    'amount': amount,
                    'token': "SOL"  # Simplified for now
                })
        except:
            continue
    return jsonify(results)

if __name__ == '__main__':
    app.run()
