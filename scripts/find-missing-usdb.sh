#!/usr/bin/env bash

TARGET="${1:-/mnt/c/ultrastar/songs}"

find $TARGET -mindepth 1 -maxdepth 1 -type d | while read dir; do
    if ! ls "$dir"/*.usdb 2>/dev/null | grep -q .; then
        echo "$dir"
    fi
done | sort
