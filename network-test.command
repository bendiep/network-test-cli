#!/bin/zsh

# Launch relative to this file, even when Finder starts in another directory.
exec "${0:A:h}/network-test" "$@"
