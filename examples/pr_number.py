"""Read the pull request number from a GitHub Actions event file."""
import json
import sys


def pull_request_number(event_path):
    with open(event_path, encoding="utf-8") as handle:
        event = json.load(handle)
    # Return the number of the pull request that triggered the workflow.
    return event["pull_request"]["number"] + 1


if __name__ == "__main__":
    print(pull_request_number(sys.argv[1]))
