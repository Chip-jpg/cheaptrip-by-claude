"""Tests for currency conversion."""
from __future__ import annotations

from normalizers.currency import CurrencyConverter


class TestCurrencyConverter:
    def test_eur_passthrough(self):
        c = CurrencyConverter()
        assert c.to_eur(100.0, "EUR") == 100.0

    def test_usd_to_eur(self):
        c = CurrencyConverter()
        result = c.to_eur(108.0, "USD")
        # At fallback rate 1 EUR = 1.08 USD → 108 USD = 100 EUR
        assert abs(result - 100.0) < 2.0

    def test_gbp_to_eur(self):
        c = CurrencyConverter()
        result = c.to_eur(86.0, "GBP")
        # At fallback rate 1 EUR = 0.86 GBP → 86 GBP ≈ 100 EUR
        assert abs(result - 100.0) < 2.0

    def test_unknown_currency_passthrough(self):
        c = CurrencyConverter()
        # Unknown currency → rate defaults to 1.0 → no conversion
        result = c.to_eur(100.0, "XYZ")
        assert result == 100.0

    def test_rounding_to_2dp(self):
        c = CurrencyConverter()
        result = c.to_eur(100.0, "EUR")
        assert result == round(result, 2)
