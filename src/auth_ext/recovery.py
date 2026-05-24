"""Recovery-code extension notes.

TODO:
- Generate one-time recovery codes and store only hashed values.
- Replace code sets atomically when users regenerate recovery codes.
- Consume codes exactly once during advanced-authentication challenge completion.
- Define recovery policy for accounts with no usable second factor.
"""
