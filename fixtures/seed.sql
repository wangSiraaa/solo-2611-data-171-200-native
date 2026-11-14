CREATE TABLE window_events(event_id integer PRIMARY KEY, account text NOT NULL, tick integer, amount integer);
INSERT INTO window_events VALUES
(1,'A',1,10),(2,'A',1,20),(3,'A',2,NULL),(4,'A',4,7),(5,'A',NULL,3),(6,'A',NULL,8),(7,'B',1,5),(8,'B',3,6),(9,'B',3,9);
