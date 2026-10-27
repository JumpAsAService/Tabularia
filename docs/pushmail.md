# Pushmail: datasources by email from a flow group

For whoever sets up flow groups that send data to people.

A **Pushmail** cell of a [flow group](capabilities/flow-groups.md) sends one email through an SMTP connection, with
**one `.xlsx` file per datasource**. It runs as a step of the group like any other cell:
after the steps before it (a refresh, the flows that publish the datasources), on the
group's schedule, and its failure stops the group.

## Before you start

- An SMTP connection exists and you have **CONNECT** on its folder (the same right the
  email of an Output node needs). Its **allowed domains** limit who may receive the email.
- You can read the datasources you attach (**VIEW** on their folders).

The group's catalog offers **Pushmail (datasources by email)** when at least one SMTP
connection is available to you; so does the picker of an empty cell.

## The cell

| Field | |
|---|---|
| **Name** | what the cell shows in the grid |
| **SMTP connection** | one you may send through |
| **To**, **Cc** | addresses separated by commas; at least one in To |
| **Subject** | text with the date placeholders, `Sales {today:yyyy-MM-dd}`; `{flow}` is the group's name |
| **Message** | plain text, or HTML when ticked (sent with a plain-text alternative) |
| **Datasources** | one file each, up to 20: the datasource, the **file name** (a template, `.xlsx` added) and the **rows at most** (1 to 1,000,000; 10,000 by default) |

The placeholders are those of the Outputs ([placeholders](placeholders.md)): `{today}`,
`{today-1:yyyyMMdd}`, `{now:HHmm}`, `{run_id}`, `{flow}`. The panel shows what each name
gives now.

**Rows at most.** A datasource with more rows sends its first ones, and the email says
so at the end of the message, in the language of whoever runs the group:

```
Not every row is attached:
sales: the first 10,000 rows of 48,210.
```

**Size.** With more than one datasource, or one sending more than 50,000 rows, the panel
warns that the email may be large. Every SMTP relay limits the size of a message (often
10–25 MB, attachments included, and base64 makes them a third larger): ask the
administrators what yours allows. Tabularia never sends more than 15 MB of files in one
email: over it the cell fails, saying so, and nothing is sent.

## What is checked, and when

When the group is saved, a Pushmail cell that is new or changed is checked with the
rights of whoever saves it: the connection is SMTP and they have CONNECT on it, every
address is an address and within the connection's allowed domains, every datasource
exists and they can read it, none twice.

When the group runs, all of it is checked again with the rights of whoever runs it (the
author of the schedule, for a scheduled run). A datasource with no data yet (never
imported or published) fails the cell before anything is sent.

A change to a Pushmail cell made by someone other than the author of the group's
schedule switches the schedule off, as for any cell added by someone else: the schedule
would otherwise send data to recipients its author never chose.

Every email is written to the audit log (`email.send`) when it leaves: who, through
which connection, to whom, which datasources.

## The run

Each send is a run of its own under the group's run (`pushmail`), with the outcome: the
recipients, the subject, the files and their sizes, the rows sent and the ones cut. The
cell in the grid shows its state and, when it failed, why (the relay refused the
message, the files were over the size cap, a recipient outside the allowed domains).
