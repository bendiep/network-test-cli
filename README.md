# Network Test CLI

A simple CLI tool for testing network connections on macOS. Uses tools already included with your Mac.

## How to use

### Run a command

Replace `<address>` and `<port>` (optional) with the values you want to test.

```sh
./network-test <address> <port>
```

### Open network-test.command

1. In Finder, open `network-test.command` (a Terminal window will open)
2. Follow the prompts (address and optional port)

Note: Some servers ignore ping, so a failed ping does not always mean the connection is broken.

## For developers

Tests are stored in `tests/`. Python 3.8 or newer is required to run them. The tests make no outside network connections.
```sh
python3 -m unittest discover -s tests -v
```
