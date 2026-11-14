CREATE TABLE events(event_id integer PRIMARY KEY,customer_id integer NOT NULL,tick integer,label text,active boolean);
INSERT INTO events VALUES (1,1,10,'alpha',true),(2,1,10,'beta',false),(3,1,NULL,'none',NULL),(4,2,4,'x',true),(5,2,6,NULL,false),(6,3,NULL,'last',NULL);
