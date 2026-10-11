"""Replies from screenshots: the editor pastes screenshots of a post they found on X, the
app writes one reply, and the editor posts it by hand and logs its link.

prompt.py (pure: the writer's prompt and its answer), checks.py (pure: the lines a reply
may never cross), writer.py (the one Claude call per attempt, with the screenshots
attached), store.py (reply_requests and the screenshots on disk), web.py + templates/
(/replies, included into the control panel). Nothing here posts.
"""
