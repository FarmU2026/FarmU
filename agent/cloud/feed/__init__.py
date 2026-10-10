"""Live feed (WS13, ticket T28): EventBridge events -> FeedMessages -> AppSync Events channels.

The projector Lambda (`handler.handler`) is the ONLY publisher to the live feed. See README.md in this folder.
"""
