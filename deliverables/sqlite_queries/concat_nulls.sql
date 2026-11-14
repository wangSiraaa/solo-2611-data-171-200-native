SELECT event_id, ('name=' || COALESCE(CAST(label AS TEXT), '') || ';') AS caption FROM events ORDER BY tick DESC NULLS LAST, event_id ASC NULLS FIRST;
