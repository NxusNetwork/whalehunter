from flask import Flask, jsonify
from flask_cors import CORS
from solana.rpc.api import Client

app = Flask(__name__)
CORS(app)
client = Client("https://api.mainnet-beta.solana.com")

# Fake "big transaction" threshold in lamports:
# 1 SOL = 1,000,000,000 lamports.
# If you really want $10k, you need to find current SOL price and do a conversion.
BIG_TX_LAMPORTS = 10000000000  # = 10 SOL, for example

@app.route('/transactions')
def get_transactions():
    # How many blocks back to scan:
    blocks_to_check = 5

    # Get the current slot (like latest block number)
    latest_slot_resp = client.get_slot()
    if not latest_slot_resp["result"]:
        return jsonify([])

    latest_slot = latest_slot_resp["result"]
    big_transactions = []

    # Go backwards through the last few blocks
    for slot in range(latest_slot, latest_slot - blocks_to_check, -1):
        block_resp = client.get_block(slot)
        block_data = block_resp.get("result")
        if not block_data or "transactions" not in block_data:
            continue

        # Each "transaction" here includes meta, signatures, etc.
        for tx in block_data["transactions"]:
            meta = tx.get("meta")
            transaction = tx.get("transaction")

            if not meta or not transaction:
                continue

            pre_balances = meta.get("preBalances", [])
            post_balances = meta.get("postBalances", [])
            if not pre_balances or not post_balances:
                continue

            # For simplicity, just compare the 0th account’s balance change
            diff = post_balances[0] - pre_balances[0]

            # If diff is larger than "threshold"
            if diff >= BIG_TX_LAMPORTS:
                # The 0th key is often the fee-payer or main signer
                account_keys = transaction["message"].get("accountKeys", [])
                wallet = account_keys[0] if account_keys else "unknown"

                # Make up a "token" name for now; real parsing is more involved
                big_transactions.append({
                    "wallet": wallet,
                    "amount": diff,  # in lamports
                    "token": "SOL (?)",  # or some default
                    "slot": slot
                })

    return jsonify(big_transactions)

if __name__ == '__main__':
    app.run(debug=True)
