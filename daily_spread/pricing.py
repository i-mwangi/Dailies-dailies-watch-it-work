import math
from typing import Optional

RISK_FREE = 0.045


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1(spot: float, strike: float, t: float, vol: float, rate: float) -> float:
    return (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))


def black_scholes(spot: float, strike: float, t: float, vol: float,
                  is_call: bool, rate: float = RISK_FREE) -> float:
    if t <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        intrinsic = (spot - strike) if is_call else (strike - spot)
        return max(intrinsic, 0.0)

    d1 = _d1(spot, strike, t, vol, rate)
    d2 = d1 - vol * math.sqrt(t)
    discount = math.exp(-rate * t)

    if is_call:
        return spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
    return strike * discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_vol(price: float, spot: float, strike: float, t: float,
                is_call: bool, rate: float = RISK_FREE) -> Optional[float]:
    if price <= 0 or t <= 0 or spot <= 0 or strike <= 0:
        return None

    intrinsic = max((spot - strike) if is_call else (strike - spot), 0.0)
    if price < intrinsic - 0.01:
        return None

    low, high = 0.005, 5.0
    if black_scholes(spot, strike, t, high, is_call, rate) < price:
        return None

    for _ in range(60):
        mid = 0.5 * (low + high)
        if black_scholes(spot, strike, t, mid, is_call, rate) < price:
            low = mid
        else:
            high = mid
        if high - low < 1e-5:
            break

    return 0.5 * (low + high)


def delta(spot: float, strike: float, t: float, vol: float,
          is_call: bool, rate: float = RISK_FREE) -> float:
    if t <= 0 or vol <= 0:
        if is_call:
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0

    d1 = _d1(spot, strike, t, vol, rate)
    return _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0
