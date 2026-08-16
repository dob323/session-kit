"""Delivery clients for the vendors' own message channels.

One module per vendor channel, each answering the same question: did this
message reach that session, and if not, exactly what stopped it. "Exactly" is
the point -- the failure this package exists to end is one report ("not
registered (possible trust prompt)") standing in for a missing binary, a dead
session, a refused socket and a real trust prompt alike.
"""
