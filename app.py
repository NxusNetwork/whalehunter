import requests
from flask import Flask, jsonify
from flask_cors import CORS
from solana.rpc.api import Client

app = Flask(__name__)
CORS(app)

SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"
client = Client(SOLANA_RPC_URL)

# ------------------------------------------------------------
# 1) LOAD THE SOLANA TOKEN LIST & BUILD MINT -> COINGECKO_ID
# ------------------------------------------------------------
TOKEN_LIST_URL = (
    "https://raw.githubusercontent.com/solana-labs/token-list/main/src/tokens/solana.tokenlist.json"
)

@app.before_first_request
def load_token_list():
    """
    Pulls down the official Solana token list JSON and builds:
       app.config["MINT_TO_COINGECKO"] = {
           <mint address>: <coingeckoId>,
           ...
       }
    Only tokens that have a "coingeckoId" are included.
    """
    try:
        resp = requests.get(TOKEN_LIST_URL, timeout=15)
        token_data = resp.json()
        tokens = token_data.get("tokens", [])

        mint_to_coingecko = {}
        for t in tokens:
            mint = t.get("address")
            cg_id = t.get("extensions", {}).get("coingeckoId")
            # Only store if there's a coingeckoId
            if mint and cg_id:
                mint_to_coingecko[mint] = cg_id

        app.config["MINT_TO_COINGECKO"] = mint_to_coingecko
        print(f"Loaded {len(mint_to_coingecko)} tokens with coingeckoId.")
    except Exception as e:
        print("Error loading token list:", e)
        app.config["MINT_TO_COINGECKO"] = {}

# ------------------------------------------------------------
# 2) BATCH PRICE LOOKUP FROM COINGECKO
# ------------------------------------------------------------
def fetch_coingecko_prices(token_ids):
    """
    Given a list of CoinGecko token IDs, fetch all in one request:
      GET /simple/price?ids=id1,id2,id3&vs_currencies=usd
    Returns a dict: { "id1": price, "id2": price, ... }
    """
    if not token_ids:
        return {}

    # Build a comma-separated string of unique IDs
    unique_ids = list(set(token_ids))
    joined_ids = ",".join(unique_ids)

    url = f"https://api.coingecko.com/api/v3/simple/price?ids={joined_ids}&vs_currencies=usd"
    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()
        # Format: {"id1": {"usd": x.xx}, "id2": {"usd": y.yy}, ...}
        prices = {}
        for cid, val in data.items():
            prices[cid] = val.get("usd", 0.0)
        return prices
    except:
        # If error, return empty or partial
        return {}

# ------------------------------------------------------------
# 3) PARSE SPL TOKEN TRANSFERS
# ------------------------------------------------------------
def parse_spl_transfers(tx_with_meta):
    """
    Examines each instruction in a transaction to find "transfer" or
    "transferChecked" instructions in the SPL token program.
    Returns a list of dicts: { mint, amount, source, destination, decimals }
    """
    results = []
    transaction = tx_with_meta.transaction
    meta = tx_with_meta.meta
    if not meta or not transaction:
        return results

    for instr in transaction.message.instructions:
        program_id = str(instr.program_id)
        # SPL Token Program address:
        if program_id == "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA":
            parsed = instr.parsed
            if not parsed:
                continue
            # e.g. "transfer", "transferChecked"
            itype = parsed.get("type", "")
            if itype in ("transfer", "transferChecked"):
                info = parsed.get("info", {})
                # For "transferChecked", amounts are in "tokenAmount"
                if itype == "transferChecked":
                    token_amt = info.get("tokenAmount", {})
                    raw_amount = token_amt.get("amount", 0)
                    decimals = token_amt.get("decimals", 0)
                else:
                    # "transfer" may differ in shape. Attempt best-guess
                    raw_amount = info.get("amount", 0)
                    decimals = 0

                mint = info.get("mint")
                source_wallet = info.get("source")
                dest_wallet = info.get("destination")

                # Convert raw integer amount to float with decimals
                if not isinstance(raw_amount, (int, float, str)):
                    continue
                raw_amount = float(raw_amount)
                amount = raw_amount / (10 ** decimals) if decimals else raw_amount

                results.append({
                    "mint": mint,
                    "amount": amount,
                    "source": source_wallet,
                    "destination": dest_wallet,
                    "decimals": decimals,
                })

    return results

# ------------------------------------------------------------
# 4) MAIN ROUTE: SCAN RECENT BLOCKS FOR BIG TRANSFERS
# ------------------------------------------------------------
@app.route("/transactions", methods=["GET"])
def get_transactions():
    # For demo, let's check the last 5 blocks
    blocks_to_check = 5
    big_transfers = []

    # 1) Get the latest slot from typed response
    slot_resp = client.get_slot()
    latest_slot = slot_resp.value
    if not latest_slot:
        return jsonify([])

    # 2) We'll accumulate all mint addresses found, so we can do 1 batch price fetch
    found_mints = set()

    # 3) Gather raw transfers
    all_transfers = []

    for slot in range(latest_slot, latest_slot - blocks_to_check, -1):
        block_resp = client.get_block(slot)
        block_data = block_resp.value
        if not block_data or not block_data.transactions:
            continue

        for tx_with_meta in block_data.transactions:
            transfers = parse_spl_transfers(tx_with_meta)
            for tr in transfers:
                # Keep track of minted addresses
                if tr["mint"]:
                    found_mints.add(tr["mint"])
                # Store (slot, transfer) to process after we know prices
                all_transfers.append({
                    "slot": slot,
                    **tr
                })

    # 4) Build a map from MINT -> coingecko ID
    mint_to_coingecko = app.config.get("MINT_TO_COINGECKO", {})

    # Filter only mints we actually can price
    coingecko_ids_needed = []
    for mint_addr in found_mints:
        if mint_addr in mint_to_coingecko:
            coingecko_ids_needed.append(mint_to_coingecko[mint_addr])

    # 5) Fetch all token prices in one batch call
    prices_map = fetch_coingecko_prices(coingecko_ids_needed)
    # prices_map is { "<coingecko_id>": <usd_price>, ... }

    # 6) For each transfer, compute USD value if we have a known coingecko price
    for entry in all_transfers:
        mint_addr = entry["mint"]
        coingecko_id = mint_to_coingecko.get(mint_addr)  # e.g. "usd-coin", "raydium"
        if not coingecko_id:
            # We don't know how to price this token
            continue

        token_price = prices_map.get(coingecko_id, 0.0)
        usd_value = entry["amount"] * token_price

        # 7) If it's over $10,000, keep it
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


if __name__ == "__main__":
    # When we start up, we load the token list once
    load_token_list()
    app.run(debug=True)
