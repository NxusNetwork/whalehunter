import requests
from flask import Flask, jsonify
from flask_cors import CORS
from solana.rpc.api import Client

# Create Flask app
app = Flask(__name__)
CORS(app)

# Your Solana RPC endpoint
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"
client = Client(SOLANA_RPC_URL)

# Globals to store token mapping
MINT_TO_COINGECKO = {}

# ------------------------------------------------------------
# 1) LOAD THE SOLANA TOKEN LIST & BUILD MINT -> COINGECKO_ID
# ------------------------------------------------------------
# We do this once at startup. (No more @app.before_first_request!)
def load_token_list():
    """
    Pulls down the official Solana token list JSON and builds
    a dictionary: MINT_TO_COINGECKO[mint_address] = coingecko_id
    for tokens that have a "coingeckoId" extension.
    """
    TOKEN_LIST_URL = (
        "https://raw.githubusercontent.com/solana-labs/token-list/main/src/tokens/solana.tokenlist.json"
    )
    try:
        resp = requests.get(TOKEN_LIST_URL, timeout=15)
        data = resp.json()
        tokens = data.get("tokens", [])

        count = 0
        for t in tokens:
            mint = t.get("address")
            cg_id = t.get("extensions", {}).get("coingeckoId")
            # Only store if there's a coingeckoId
            if mint and cg_id:
                MINT_TO_COINGECKO[mint] = cg_id
                count += 1
        print(f"Loaded {count} tokens with coingeckoId.")
    except Exception as e:
        print("Error loading token list:", e)

# Load it at startup
load_token_list()

# ------------------------------------------------------------
# 2) BATCH PRICE LOOKUP FROM COINGECKO
# ------------------------------------------------------------
def fetch_coingecko_prices(token_ids):
    """
    Given a list of CoinGecko token IDs, fetch them in one API call.
    Returns dict: { "usd-coin": 1.0, "raydium": 0.25, ... } in USD
    """
    if not token_ids:
        return {}

    # Unique IDs, comma-separated
    unique_ids = list(set(token_ids))
    joined_ids = ",".join(unique_ids)

    url = f"https://api.coingecko.com/api/v3/simple/price?ids={joined_ids}&vs_currencies=usd"
    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()
        prices = {}
        for cid, val in data.items():
            prices[cid] = val.get("usd", 0.0)
        return prices
    except:
        return {}

# ------------------------------------------------------------
# 3) PARSE SPL TOKEN TRANSFERS (FROM JSON-PARSED INSTRUCTIONS)
# ------------------------------------------------------------
def parse_spl_transfers(tx_dict):
    """
    tx_dict is a dict with keys like: "transaction", "meta"
      transaction -> message -> instructions
    Because we used encoding="jsonParsed", instructions look like:
      {
        "program": "spl-token",
        "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "parsed": {
          "type": "transferChecked" or "transfer",
          "info": {
             "source": "...",
             "destination": "...",
             "mint": "...",
             "tokenAmount": {
               "amount": "...",
               "decimals": X
             }
          }
        }
      }
    Returns a list of:
      {
        "mint": str,
        "amount": float,
        "source": str,
        "destination": str,
      }
    """
    results = []
    # Safely get instructions
    instructions = tx_dict["transaction"]["message"].get("instructions", [])
    for instr in instructions:
        program_id = instr.get("programId", "")
        if program_id == "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA":
            parsed = instr.get("parsed", {})
            itype = parsed.get("type", "")
            info = parsed.get("info", {})

            # "transfer" or "transferChecked"
            if itype in ("transfer", "transferChecked"):
                # The minted address
                mint = info.get("mint")
                source = info.get("source")
                destination = info.get("destination")

                # For "transferChecked", the amount is in "tokenAmount"
                # For "transfer", sometimes also "tokenAmount"
                token_amount = info.get("tokenAmount")
                if not token_amount:
                    # Some instructions might store "amount" differently
                    # This is a fallback
                    raw_amount = info.get("amount", 0)
                    decimals = 0
                else:
                    raw_amount = token_amount.get("amount", "0")
                    decimals = token_amount.get("decimals", 0)

                # Convert to float
                raw_amount = float(raw_amount)
                real_amount = raw_amount / (10 ** decimals) if decimals else raw_amount

                results.append({
                    "mint": mint,
                    "amount": real_amount,
                    "source": source,
                    "destination": destination,
                })
    return results

# ------------------------------------------------------------
# 4) MAIN ROUTE: SCAN RECENT BLOCKS FOR BIG TRANSFERS OVER $10K
# ------------------------------------------------------------
@app.route("/transactions", methods=["GET"])
def get_transactions():
    blocks_to_check = 5
    big_transfers = []

    # 1) Get the latest slot
    slot_resp = client.get_slot()
    latest_slot = slot_resp.value
    if not latest_slot:
        return jsonify([])

    all_transfers = []
    found_mints = set()

    # 2) Loop over recent blocks
    for slot in range(latest_slot, latest_slot - blocks_to_check, -1):
        # Tell Solana we accept version 0 txs & want JSON-parsed instructions
        block_resp = client.get_block(
            slot,
            max_supported_transaction_version=0,
            encoding="jsonParsed"
        )
        block_data = block_resp.value
        if not block_data or "transactions" not in block_data:
            continue

        # Each item in block_data["transactions"] has "transaction", "meta"
        for tx_dict in block_data["transactions"]:
            # parse_spl_transfers to find SPL token movements
            transfers = parse_spl_transfers(tx_dict)
            if not transfers:
                continue

            for t in transfers:
                mint_addr = t["mint"]
                amount = t["amount"]
                source = t["source"]
                destination = t["destination"]
                if mint_addr:
                    found_mints.add(mint_addr)

                # We'll store them, then compute USD value later
                all_transfers.append({
                    "slot": slot,
                    "mint": mint_addr,
                    "amount": amount,
                    "source": source,
                    "destination": destination,
                })

    # 3) Figure out which mints we can price (based on MINT_TO_COINGECKO)
    coingecko_ids_needed = []
    for mint in found_mints:
        if mint in MINT_TO_COINGECKO:
            coingecko_ids_needed.append(MINT_TO_COINGECKO[mint])

    # 4) Fetch prices in one batch call
    prices_map = fetch_coingecko_prices(coingecko_ids_needed)
    # e.g. {"usd-coin": 1.0, "raydium": 0.25, ...}

    # 5) Calculate USD value for each transfer & find those > $10k
    for entry in all_transfers:
        mint_addr = entry["mint"]
        if mint_addr not in MINT_TO_COINGECKO:
            continue
        coingecko_id = MINT_TO_COINGECKO[mint_addr]

        token_price = prices_map.get(coingecko_id, 0.0)
        usd_value = entry["amount"] * token_price

        if usd_value >= 10_000:
            big_transfers.append({
                "slot": entry["slot"],
                "mint": mint_addr,
                "amount": entry["amount"],
                "usd_value": usd_value,
                "source": entry["source"],
                "destination": entry["destination"],
                "coingecko_id": coingecko_id,
                "token_price": token_price
            })

    return jsonify(big_transfers)

# ------------------------------------------------------------
# RUN APP (if using "gunicorn app:app" on Heroku, no need for this)
# ------------------------------------------------------------
if __name__ == "__main__":
    # Just for local testing
    app.run(debug=True)
