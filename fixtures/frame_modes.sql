-- Manual frame tables supplementing fixtures/seed.sql.
--
-- These rows are deliberately shaped so ROWS, GROUPS, and RANGE produce
-- different frames on the same data.  The table is a separate single table in
-- the same dedicated test database; the fixture table itself is never
-- modified.  Insertion order is intentionally scrambled relative to event_id
-- to exercise the "insertion order must not change determined results"
-- requirement.

DROP TABLE IF EXISTS frame_modes;
CREATE TABLE frame_modes(
    event_id integer PRIMARY KEY,
    account text NOT NULL,
    tick integer,
    amount integer
);

INSERT INTO frame_modes VALUES
(9, 'A', 5, 90),
(3, 'A', 3, 30),
(1, 'A', 1, 10),
(6, 'A', NULL, 60),
(4, 'A', 4, NULL),
(2, 'A', 1, 20),
(8, 'A', 5, 80),
(5, 'A', 4, 40),
(7, 'A', 5, NULL),
(12, 'B', 2, 12),
(11, 'B', 2, 11),
(10, 'B', 1, 10);
