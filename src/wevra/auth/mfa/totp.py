"""TOTP extension notes.

TODO:
- Define enrolment secret generation and display payload shape.
- Confirm pending credentials with a valid code before activation.
- Enforce replay protection and an accepted time-window policy during login.
- Provide disablement hooks that can require a current password or second factor.
"""
