"""
yoshimulib - Aerospace Engineering Library for Spacecraft Dynamics

⚠ これは mRendering に同梱した yoshimulib の**部分集合**です。
   出典・除外したモジュール・更新手順は VENDORED.md を参照。
   本家: https://github.com/yyoshimula/yoshimulib

Python conversion from yMATLAB/
A comprehensive library for spacecraft dynamics, attitude control, orbital mechanics,
and space environment modeling.

Modules (同梱分):
-----------------
attitude
    Attitude dynamics, DCM, quaternions, Euler angles, kinematics
conversion
    Unit and calendar conversions
math_utils
    Mathematical utilities (skew matrix, wrap_pi, Legendre polynomials)
time_utils
    Time system conversions (JD, MJD, UTC, TT, leap seconds)
orbit
    Orbital mechanics, Kepler's equation, coordinate transforms
sun_moon
    Solar and lunar ephemerides

Author: Yasuhiro Yoshimura (y.yoshimula@gmail.com)
"""

__version__ = "1.0.0"
__author__ = "Yasuhiro Yoshimura"

def passfail(passed: bool) -> str:
    """
    # MATLAB-compatible helper (MATLAB: passfail)
    """
    return "PASS" if passed else "FAIL"

# Import key modules for convenient access（同梱分のみ）
from . import attitude
from . import conversion
from . import math_utils
from . import time_utils
from . import orbit
from . import sun_moon

__all__ = [
    'attitude',
    'conversion',
    'math_utils',
    'time_utils',
    'orbit',
    'sun_moon',
    'passfail',
]
