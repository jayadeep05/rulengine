"""
instrument_service.py — High-Performance Instrument Discovery & Search Service
Provides fast in-memory indexing of NSE Equities and Options contracts
from nse_instruments.json.gz for PrimeRadar – Intraday and PrimeRadar – Options.
"""

import os
import gzip
import json
import logging
from datetime import datetime
from typing import List, Dict, Optional, Any

logger = logging.getLogger(__name__)

class InstrumentService:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(InstrumentService, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, data_path: Optional[str] = None):
        if self._initialized:
            return
        
        self.data_path = data_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "nse_instruments.json.gz"
        )
        
        # Primary in-memory index stores
        self.stocks: List[Dict[str, Any]] = []
        self.stocks_by_symbol: Dict[str, Dict[str, Any]] = {}
        self.instruments_by_key: Dict[str, Dict[str, Any]] = {}
        
        # Options index: { underlying_symbol: { expiry_epoch: [contract_dict, ...] } }
        self.options_by_underlying: Dict[str, Dict[int, List[Dict[str, Any]]]] = {}
        self.unique_underlyings: List[str] = []
        
        self._load_and_index()
        self._initialized = True

    def _load_and_index(self):
        """Loads and indexes nse_instruments.json.gz into memory."""
        if not os.path.exists(self.data_path):
            logger.error(f"Instruments file not found at: {self.data_path}")
            return

        try:
            t0 = datetime.now()
            with gzip.open(self.data_path, "rt", encoding="utf-8") as f:
                data = json.load(f)

            stock_list = []
            options_dict = {}

            # Priority indices that should appear at top of options underlying list
            priority_indices = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"]

            for item in data:
                segment = item.get("segment")
                inst_type = item.get("instrument_type")
                inst_key = item.get("instrument_key")
                trading_symbol = item.get("trading_symbol")

                if not inst_key or not trading_symbol:
                    continue

                # 1. NSE Equity Stocks (segment: NSE_EQ, instrument_type: EQ)
                if segment == "NSE_EQ" and inst_type == "EQ":
                    stock_entry = {
                        "symbol": trading_symbol,
                        "name": item.get("name", trading_symbol),
                        "instrument_key": inst_key,
                        "lot_size": int(item.get("lot_size", 1)),
                        "tick_size": float(item.get("tick_size", 0.05)),
                        "exchange_token": item.get("exchange_token", ""),
                        "isin": item.get("isin", "")
                    }
                    stock_list.append(stock_entry)
                    self.stocks_by_symbol[trading_symbol.upper()] = stock_entry
                    self.instruments_by_key[inst_key] = stock_entry

                # 2. NSE Options Contracts (segment: NSE_FO, instrument_type: CE or PE)
                elif segment == "NSE_FO" and inst_type in ("CE", "PE"):
                    underlying = (
                        item.get("underlying_symbol")
                        or item.get("asset_symbol")
                        or item.get("name")
                    )
                    expiry = item.get("expiry")
                    strike = item.get("strike_price")

                    if not underlying or expiry is None or strike is None:
                        continue

                    underlying_upper = underlying.upper()
                    expiry_int = int(expiry)

                    opt_entry = {
                        "trading_symbol": trading_symbol,
                        "underlying": underlying_upper,
                        "expiry": expiry_int,
                        "strike_price": float(strike),
                        "instrument_type": inst_type,
                        "instrument_key": inst_key,
                        "lot_size": int(item.get("lot_size", 1)),
                        "tick_size": float(item.get("tick_size", 0.05)),
                        "freeze_quantity": float(item.get("freeze_quantity", 0)),
                        "exchange_token": item.get("exchange_token", "")
                    }

                    self.instruments_by_key[inst_key] = opt_entry

                    if underlying_upper not in options_dict:
                        options_dict[underlying_upper] = {}
                    if expiry_int not in options_dict[underlying_upper]:
                        options_dict[underlying_upper][expiry_int] = []

                    options_dict[underlying_upper][expiry_int].append(opt_entry)

            # Sort stocks alphabetically by symbol
            stock_list.sort(key=lambda x: x["symbol"])
            self.stocks = stock_list

            # Sort options underlyings: priority indices first, then others alphabetically
            all_underlyings = set(options_dict.keys())
            sorted_underlyings = [idx for idx in priority_indices if idx in all_underlyings]
            remaining = sorted([u for u in all_underlyings if u not in priority_indices])
            self.unique_underlyings = sorted_underlyings + remaining
            self.options_by_underlying = options_dict

            duration = (datetime.now() - t0).total_seconds()
            logger.info(
                f"InstrumentService indexed {len(self.stocks)} stocks and "
                f"{len(self.unique_underlyings)} option underlyings in {duration:.2f}s"
            )

        except Exception as e:
            logger.error(f"Failed to load and index instruments: {e}", exc_info=True)

    def search_stocks(self, query: str, limit: int = 15) -> List[Dict[str, Any]]:
        """
        Autocomplete search for NSE stocks.
        Returns matches prioritised by:
        1. Exact symbol match
        2. Symbol prefix match
        3. Symbol substring match
        4. Company name substring match
        """
        if not query:
            return self.stocks[:limit]

        q = query.strip().upper()
        if not q:
            return self.stocks[:limit]

        exact = []
        prefix = []
        symbol_substr = []
        name_substr = []

        for s in self.stocks:
            sym = s["symbol"].upper()
            name = s["name"].upper()

            if sym == q:
                exact.append(s)
            elif sym.startswith(q):
                prefix.append(s)
            elif q in sym:
                symbol_substr.append(s)
            elif q in name:
                name_substr.append(s)

            if len(exact) + len(prefix) + len(symbol_substr) + len(name_substr) >= limit * 3:
                break

        # Combine results with strict deduplication
        seen = set()
        results = []
        for group in (exact, prefix, symbol_substr, name_substr):
            for item in group:
                if item["symbol"] not in seen:
                    seen.add(item["symbol"])
                    results.append(item)
                    if len(results) >= limit:
                        return results

        return results

    def get_stock(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Direct lookup of stock by symbol."""
        return self.stocks_by_symbol.get(symbol.strip().upper())

    def get_options_underlyings(self) -> List[str]:
        """Returns sorted list of option underlying symbols."""
        return self.unique_underlyings

    def get_options_expiries(self, underlying: str) -> List[Dict[str, Any]]:
        """
        Returns sorted list of available expiry dates for an underlying.
        Returns: [ { "expiry": epoch_ms, "date": "YYYY-MM-DD", "formatted": "DD MMM YYYY" } ]
        """
        underlying_upper = underlying.strip().upper()
        expiries_map = self.options_by_underlying.get(underlying_upper, {})
        sorted_epochs = sorted(list(expiries_map.keys()))

        results = []
        for epoch_ms in sorted_epochs:
            dt = datetime.fromtimestamp(epoch_ms / 1000.0)
            results.append({
                "expiry": epoch_ms,
                "date": dt.strftime("%Y-%m-%d"),
                "formatted": dt.strftime("%d %b %Y")
            })
        return results

    def get_options_contracts(
        self,
        underlying: str,
        expiry: Optional[int] = None,
        option_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Returns available options contracts for given underlying, expiry epoch, and CE/PE.
        Contracts are sorted by strike price.
        """
        underlying_upper = underlying.strip().upper()
        expiries_map = self.options_by_underlying.get(underlying_upper, {})

        if not expiries_map:
            return []

        # If expiry not supplied, default to the earliest available expiry
        target_expiry = int(expiry) if expiry is not None else sorted(expiries_map.keys())[0]
        contracts = expiries_map.get(target_expiry, [])

        if option_type:
            opt_type_upper = option_type.strip().upper()
            contracts = [c for c in contracts if c["instrument_type"] == opt_type_upper]

        # Sort contracts by strike price
        sorted_contracts = sorted(contracts, key=lambda c: (c["strike_price"], c["instrument_type"]))
        return sorted_contracts

    def get_instrument_by_key(self, instrument_key: str) -> Optional[Dict[str, Any]]:
        """Lookup instrument info by token/instrument_key."""
        return self.instruments_by_key.get(instrument_key)


# Global singleton instance
instrument_service = InstrumentService()
