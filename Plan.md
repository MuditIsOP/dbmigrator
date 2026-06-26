# Production Database Migration Tool - Engineering Design Specification (EDS)

> **Version:** 1.0
> **Status:** Implementation Blueprint

---

# 1. Objective

Build a **production-grade desktop application** that copies one or more MySQL databases from AWS RDS to Azure Database for MySQL.

The application is **NOT** a synchronization tool.

The application is **NOT** a backup tool.

The application is **NOT** a deployment tool.

Its only responsibility is to:

1. Connect to AWS MySQL.
2. Discover available databases.
3. Allow the user to select databases.
4. Create identical databases on Azure.
5. Copy every supported schema object.
6. Copy every row of data.
7. Verify every copied object.
8. Produce a detailed report.
9. Never delete or modify the source database.
10. Never report SUCCESS unless every verification succeeds.

---

# 2. Critical Safety Rules

These rules are absolute.

- Never execute DROP DATABASE.
- Never execute DROP TABLE.
- Never execute DELETE on the source.
- Never execute UPDATE on the source.
- Never truncate tables.
- Never overwrite an existing Azure database without explicit confirmation.
- Never mark migration successful if verification fails.
- Every failure must be logged.
- Every operation must be resumable.

The source database is always treated as READ ONLY.

---

# 3. High Level Workflow

Connect AWS
→ Connect Azure
→ Discover databases
→ Populate dropdown
→ User selects database(s)
→ Read metadata
→ Validate destination
→ Create database
→ Create schema
→ Copy data
→ Copy routines
→ Copy triggers
→ Copy events
→ Verify schema
→ Verify data
→ Generate reports
→ Finished

---

# 4. UI Requirements

Main Window

- AWS Connection Panel
- Azure Connection Panel
- Test Connection buttons
- Refresh Database List
- Search box
- Multi-select database list
- Destination database name (default = same as source)
- Dry Run checkbox
- Resume previous migration
- Start button
- Cancel button
- Live logs
- Progress bars
- ETA
- Current database
- Current table
- Verification status

---

# 5. Database Discovery

Read all non-system databases.

Ignore:

- mysql
- information_schema
- performance_schema
- sys

For every database collect:

- Name
- Charset
- Collation
- Size
- Table count
- View count
- Procedure count
- Function count
- Trigger count
- Event count

Save as discovery.json.

---

# 6. Objects To Copy

Must copy exactly:

Database
Tables
Columns
Primary Keys
Foreign Keys
Unique Keys
Indexes
Check Constraints
Column comments
Table comments
Charsets
Collations
Storage engine
AUTO_INCREMENT
Generated columns
Views
Stored Procedures
Functions
Triggers
Events
Data
BLOB
TEXT
JSON
Spatial types (if present)

---

# 7. Copy Order

Create database

↓

Tables

↓

Indexes

↓

Primary Keys

↓

Data

↓

Foreign Keys

↓

Views

↓

Functions

↓

Procedures

↓

Triggers

↓

Events

---

# 8. Data Copy

Use batch copy.

Never load an entire huge table into memory.

Configurable batch size.

Track:

Rows copied

Rows remaining

Transfer speed

ETA

Resume point

---

# 9. Verification

A migration is considered valid ONLY IF every verification passes.

Verify:

✓ Database exists

✓ Charset

✓ Collation

✓ Table count

✓ Column count

✓ Column names

✓ Data types

✓ Length

✓ Precision

✓ Scale

✓ Nullable

✓ Default values

✓ Generated expressions

✓ AUTO_INCREMENT

✓ Primary Keys

✓ Foreign Keys

✓ Unique Keys

✓ Indexes

✓ Constraints

✓ Engine

✓ Table options

✓ Views

✓ Procedures

✓ Functions

✓ Triggers

✓ Events

✓ Row counts

✓ Data checksum / deterministic hash

✓ BLOB integrity

✓ JSON values

✓ Unicode values

If any mismatch exists:

Migration Status = FAILED

Never SUCCESS.

---

# 10. Source Change Detection

If source row counts or checksums change during migration:

Status = SOURCE CHANGED DURING MIGRATION

Do not mark success.

---

# 11. Logging

Maintain:

logs/migration.log
logs/error.log
logs/verification.log
logs/performance.log

Every log entry contains:

Timestamp

Database

Table

Operation

Duration

Result

Exception

---

# 12. Reports

Generate:

report.json

report.html

Summary:

Start Time

Finish Time

Duration

Database Size

Tables

Rows

Failures

Warnings

Verification Result

Overall Status

---

# 13. Resume

Persist checkpoints after every successfully completed table.

On restart:

Offer Resume.

Never repeat completed work unless requested.

---

# 14. Error Handling

Retry transient network failures.

Abort on:

Schema mismatch

Permission denied

Corrupted dump

Import failure

Verification failure

Checksum mismatch

Lost connection after retry limit

Always provide actionable error messages.

---

# 15. Performance

- Stream data where possible.
- Configurable worker count.
- Limit memory usage.
- Show live throughput.
- Support multi-GB databases.

Correctness has higher priority than speed.

---

# 16. Security

Store credentials securely.

Never print passwords.

Support SSL if configured.

Mask secrets in logs.

---

# 17. Suggested Project Structure

src/
    ui/
    services/
    migration/
    verification/
    reporting/
    models/
    utils/
config/
logs/
reports/
state/
tests/

---

# 18. Acceptance Criteria

SUCCESS requires ALL of the following:

- Database created.
- Schema identical.
- Object counts identical.
- Data copied.
- Row counts identical.
- Checksums/hashes pass.
- All supported objects verified.
- No unrecovered errors.
- Final verification complete.

Otherwise:

FAILED

---

# 19. Edge Cases

Handle:

- Empty databases
- Empty tables
- Very large tables
- Huge BLOB columns
- Long TEXT fields
- Unicode
- Emojis
- NULL values
- Zero dates (where supported)
- Decimal precision
- Timezone values
- Composite primary keys
- Composite foreign keys
- Circular FK dependencies
- Reserved keywords
- Case-sensitive identifiers
- Tables without primary keys
- AUTO_INCREMENT gaps
- Duplicate indexes
- Network interruption
- Disk full
- Azure quota exceeded
- AWS timeout
- Authentication expiry
- Existing destination database
- Existing destination table
- Interrupted migration
- Read-only Azure configuration
- Unsupported object types (report clearly)

---

# 20. Guiding Principle

This application is a COPY + VERIFY tool.

Its responsibility is to maximize correctness, traceability, and recoverability.

It must prefer failing safely over silently producing an incorrect migration.

Never claim success unless verification demonstrates that the copied database matches the source according to all implemented validation checks.

End of document.
