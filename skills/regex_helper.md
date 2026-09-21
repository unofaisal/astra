# For writing and debugging regex patterns — from basics to advanced.

# Regex Helper

A guide for writing, testing, and debugging regular expressions.

## Basic Patterns

### Anchors
- `^` — Start of string
- `$` — End of string
- `\b` — Word boundary

### Character Classes
- `[abc]` — Any one of a, b, or c
- `[^abc]` — Any character NOT a, b, or c
- `[a-z]` — Any lowercase letter
- `\d` — Any digit (0-9)
- `\w` — Any word character (letter, digit, underscore)
- `\s` — Any whitespace

### Quantifiers
- `*` — Zero or more
- `+` — One or more
- `?` — Zero or one
- `{n}` — Exactly n times
- `{n,}` — n or more times
- `{n,m}` — Between n and m times

## Common Patterns

### Email (simplified)
```
[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}
```

### URL
```
https?://[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(/.*)?
```

### Phone number
```
\+?1?[-.\s]?\(?[0-9]{3}\)?[-.\s]?[0-9]{3}[-.\s]?[0-9]{4}
```

### Date (YYYY-MM-DD)
```
[0-9]{4}-[0-9]{2}-[0-9]{2}
```

## Tips
- Test patterns incrementally — build up complexity step by step.
- Use online tools like regex101.com to test.
- Add comments in verbose mode (Python: `re.VERBOSE`, JavaScript: `x` flag).
- Prefer readability over cleverness — a clear regex beats a cryptic one.
- When in doubt, escape special characters with `\`.

## Common Pitfalls
- Greedy vs. non-greedy matching (`.*` vs `.*?`).
- Forgetting to escape dots, parentheses, or brackets.
- Not accounting for newlines with `.` (use `re.DOTALL` in Python).
- Overcomplicating — sometimes string methods are simpler.
