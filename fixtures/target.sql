CREATE TABLE events(event_id INTEGER PRIMARY KEY,customer_id INTEGER NOT NULL,tick INTEGER,label TEXT,active INTEGER CHECK(active IN (0,1)));
