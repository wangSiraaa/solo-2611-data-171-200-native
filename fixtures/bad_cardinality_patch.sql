-- Apply to a disposable copy with constraints deliberately removed for validation testing.
CREATE TABLE customers_bad AS SELECT * FROM customers;
INSERT INTO customers_bad VALUES ('C1','east');
DROP TABLE customers;
ALTER TABLE customers_bad RENAME TO customers;
