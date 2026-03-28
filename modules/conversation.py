# Chat with Hermes is handled entirely through the web UI at /chat (web/app.py).
# The Twilio SMS polling functions (poll_incoming, process_and_respond) that used
# to live here were removed when SMS polling was dropped in favour of the web chat
# interface. The generate_chat_response helper remains in modules/analyzer.py and
# is imported directly by web/app.py.
