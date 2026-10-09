-- SPEC-05: a scratch schema for the drift tests. Not part of any source's selected tables; the tests
-- create their own tables here, add them to a throwaway source, change them, and drop them.
CREATE SCHEMA drift AUTHORIZATION nn_drift;
REVOKE ALL ON SCHEMA drift FROM PUBLIC;
GRANT USAGE ON SCHEMA drift TO nn_reader;
GRANT CONNECT ON DATABASE nn_client TO nn_drift;
-- nn_reader (never the owner) gets SELECT on whatever nn_drift creates here.
ALTER DEFAULT PRIVILEGES FOR ROLE nn_drift IN SCHEMA drift GRANT SELECT ON TABLES TO nn_reader;
-- Lets nn_drift hand a scratch table to nn_reader (ALTER ... OWNER TO needs membership in the new owner),
-- which is the "owner changed" drift case. Only the scratch schema is owned by nn_drift.
GRANT nn_reader TO nn_drift;
