"""Source/frozen helper dispatch without loading the app or personal database."""
def dispatch_helper(arguments, **_):
    if arguments[:1] != ['--twitter-worker']:
        return None
    from modules.twitter.worker import main
    return main(arguments[1:])
