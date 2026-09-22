# For writing and optimizing SQL queries — from simple selects to complex joins.

# SQL Helper

A guide for writing clear, efficient SQL queries.

## Basic Query Structure
```sql
SELECT column1, column2
FROM table
WHERE condition
GROUP BY column
HAVING condition
ORDER BY column
LIMIT count;
```

## Common Operations

### Filtering
```sql
-- Exact match
WHERE name = 'Alice'

-- Multiple options
WHERE status IN ('active', 'pending')

-- Range
WHERE date BETWEEN '2024-01-01' AND '2024-12-31'

-- Pattern matching
WHERE email LIKE '%@gmail.com'

-- NULL checks
WHERE deleted_at IS NULL
```

### Joins
```sql
-- Inner join (only matching rows)
SELECT * FROM users u
INNER JOIN orders o ON u.id = o.user_id

-- Left join (all from left, matching from right)
SELECT * FROM users u
LEFT JOIN orders o ON u.id = o.user_id

-- Multiple joins
SELECT u.name, COUNT(o.id) as order_count
FROM users u
LEFT JOIN orders o ON u.id = o.user_id
GROUP BY u.id, u.name
```

### Aggregation
```sql
SELECT department, AVG(salary) as avg_salary
FROM employees
GROUP BY department
HAVING AVG(salary) > 50000
ORDER BY avg_salary DESC
```

### Subqueries
```sql
-- Subquery in WHERE
SELECT * FROM products
WHERE price > (SELECT AVG(price) FROM products)

-- Subquery in SELECT
SELECT name, 
       (SELECT COUNT(*) FROM orders WHERE orders.user_id = users.id) as order_count
FROM users
```

## Performance Tips
- Index columns used in WHERE, JOIN, and ORDER BY.
- Avoid `SELECT *` — only select needed columns.
- Use EXPLAIN to check query plans.
- Be careful with LIKE '%keyword' — it can't use indexes.
- Consider partitioning large tables.

## Common Pitfalls
- Forgetting to GROUP BY when using aggregates.
- Using SELECT DISTINCT when GROUP BY is more appropriate.
- Not handling NULLs in comparisons.
- Over-joining and creating duplicate rows.
