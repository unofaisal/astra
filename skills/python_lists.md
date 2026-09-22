# Python list operations — comprehensions, filtering, map/filter/reduce, and common patterns

# Python Lists — Core Patterns

## List Comprehensions
```python
# Basic: squares of 0..9
squares = [x**2 for x in range(10)]

# With condition: even squares
even_squares = [x**2 for x in range(10) if x % 2 == 0]

# Nested: flatten a matrix
matrix = [[1,2],[3,4],[5,6]]
flat = [num for row in matrix for num in row]
```

## Filtering & Map/Filter/Reduce
```python
# filter
nums = [1,2,3,4,5]
evens = list(filter(lambda x: x % 2 == 0, nums))

# map
doubled = list(map(lambda x: x * 2, nums))

# reduce
from functools import reduce
product = reduce(lambda a, b: a * b, nums)
```

## Common Patterns
- **Finding duplicates**: `len(lst) != len(set(lst))`
- **Most frequent**: `Counter(lst).most_common(1)`
- **Zip pairs**: `list(zip(a, b))`
- **enumerate**: `for i, v in enumerate(lst):`
```

