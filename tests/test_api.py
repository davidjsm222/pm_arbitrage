import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from cross_venue_arb.api import (
    _kalshi_book_message,
    _kalshi_headers,
    _polymarket_us_headers,
    parse_kalshi_market,
    parse_polymarket_us_market,
)


class MarketParsingTests(unittest.TestCase):
    def test_parses_kalshi_native_market_fields(self):
        market = parse_kalshi_market(
            {
                "ticker": "KXTEST-26-YES",
                "event_ticker": "KXTEST-26",
                "title": "Will the test happen?",
                "yes_sub_title": "Test outcome",
                "rules_primary": "Resolves Yes if the test happens.",
                "expected_expiration_time": "2026-12-31T00:00:00Z",
                "volume_fp": "123.00",
                "volume_24h_fp": "4.00",
                "status": "active",
            }
        )

        self.assertEqual(market.market_id, "KXTEST-26-YES")
        self.assertEqual(market.primary_entity_name, "Test outcome")
        self.assertEqual(market.volume, 123.0)
        self.assertIn("Resolves Yes", market.description)

    def test_parses_polymarket_us_outcome_identity_and_market_fee(self):
        market = parse_polymarket_us_market(
            {
                "id": "7899",
                "question": "World Series Champion",
                "slug": "tec-mlb-champ-2026-ath",
                "title": "Athletics",
                "description": "Will Athletics win the World Series? More rules follow.",
                "category": "sports",
                "endDate": "2026-11-06T16:20:09Z",
                "active": True,
                "closed": False,
                "feeCoefficient": 0.06,
            }
        )

        self.assertEqual(market.market_id, "tec-mlb-champ-2026-ath")
        self.assertEqual(market.name, "Will Athletics win the World Series?")
        self.assertEqual(market.primary_entity_name, "Athletics")
        self.assertEqual(market.taker_fee_coefficient, 0.06)

    def test_kalshi_no_bids_become_executable_yes_asks(self):
        message = _kalshi_book_message(
            "KXTEST",
            {
                "yes": {0.40: 12.0, 0.39: 20.0},
                "no": {0.55: 8.0, 0.54: 10.0},
            },
            "snapshot",
            {"ts": "2026-07-22T12:00:00Z"},
        )

        self.assertEqual(message.data["best_bid_price"], 0.40)
        self.assertAlmostEqual(message.data["best_ask_price"], 0.45)
        self.assertEqual(message.data["asks"][0]["qty"], 8.0)


class AuthenticationTests(unittest.TestCase):
    def test_kalshi_headers_are_rsa_pss_signed(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "kalshi.pem"
            key_path.write_bytes(pem)
            with patch.dict(
                "os.environ",
                {
                    "KALSHI_API_KEY_ID": "key-id",
                    "KALSHI_PRIVATE_KEY_PATH": str(key_path),
                },
                clear=False,
            ):
                headers = _kalshi_headers("GET", "/trade-api/ws/v2")

        message = (
            headers["KALSHI-ACCESS-TIMESTAMP"] + "GET" + "/trade-api/ws/v2"
        ).encode()
        private_key.public_key().verify(
            base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256.digest_size,
            ),
            hashes.SHA256(),
        )

    def test_polymarket_us_headers_are_ed25519_signed(self):
        private_key = ed25519.Ed25519PrivateKey.generate()
        raw_key = private_key.private_bytes_raw()
        with patch.dict(
            "os.environ",
            {
                "POLYMARKET_US_API_KEY_ID": "key-id",
                "POLYMARKET_US_SECRET_KEY": base64.b64encode(raw_key).decode(),
            },
            clear=False,
        ):
            headers = _polymarket_us_headers("GET", "/v1/ws/markets")

        message = (
            headers["X-PM-Timestamp"] + "GET" + "/v1/ws/markets"
        ).encode()
        private_key.public_key().verify(
            base64.b64decode(headers["X-PM-Signature"]), message
        )


if __name__ == "__main__":
    unittest.main()
