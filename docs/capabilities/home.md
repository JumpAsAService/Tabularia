# The home page

Tabularia opens on a page each person arranges for themselves: a grid of **widgets** that
says, without a click, what was touched last and what needs attention. It is a page of
the person, not an object of a folder: nobody else sees it, and it follows them to any
browser they sign in from.

> [!NOTE]
> The home page is on `main`, for the next minor version. The long-term support line,
> 1.4.x, opens on Explore.

## Where it is

Sign in, or click **Tabularia** at the top left from any page (from the keyboard: `g`
then `h`). **Explore**, where the folders are, is the first item of the bar (`g` then `e`).

## The page

Under the greeting, one line says the day in four numbers, each a link: the runs of
today, how many failed, what is running now and what is due in the next 24 hours. Under
it, your widgets.

## The widgets

| Widget | What it shows | Its options |
|---|---|---|
| **Clock** | The time and the date, with the time zone | The zone (the browser's, or one you choose), 12 or 24 hours, the seconds |
| **Shortcuts** | Buttons to the pages you use most | Which pages |
| **Favourites** | The objects you pinned: in Explore, a row's menu (`m`, or a right click) has **Add to home** | The star on a row unpins it |
| **Recent** | The objects you changed last, of any kind, with their folder | How many (5 to 20), and which kinds |
| **Flow runs** | The last runs of each flow as bars, oldest on the left: a bar is as tall as its run lasted and green, red or blue as it succeeded, failed or is still going. Beside them, the median duration and how many failed | Up to five flows (none chosen: the flows that ran most in the last 30 days); how many runs each (5 to 50) |
| **Runs by day** | One column per day, split into runs that succeeded and runs that failed, with the total and the share that succeeded | The days (7 to 90) |
| **Running now** | What is running or waiting to start | – |
| **Failed runs** | The last runs that failed, each with the first line of its error | Yours alone or all those you may see; how many days back (1 to 30) |
| **Next runs** | The schedules due next, soonest first, with their cron: flows, flow groups, scripts, SQL scripts and datasource refreshes | Yours alone or all those you may see; how many hours ahead (1 to 168) |
| **Datasources** | When each datasource was last refreshed and how many rows it holds | Up to ten datasources (none chosen: those refreshed last) |
| **Notices** | What the administrators posted for everyone | – |
| **Note** | A text of your own: a reminder, a link | The text, with headings, lists, bold and code as in Markdown |

For administrators, three more:

| Widget | What it shows |
|---|---|
| **Queue** | Jobs running and waiting, the workers that answer and how busy they are |
| **Sessions** | Who is signed in, those online first |
| **Backups** | How the last backup of Tabularia's database went, and when the next is due |

A widget reads the same data as the page it mirrors, with your rights: *Failed runs*
lists what the **Runs** page would list for you, *Flow runs* only flows you may open,
*Favourites* only what you may still see. Putting a widget on the page never shows more
than you may already see, and a widget of the administrators is not offered to anyone
else. The lists read again by themselves while the page is in view (every minute; the
queue and what is running every ten seconds).

In the charts the colour is never the only sign: every bar and every column says its
day, its duration and how it ended when the pointer is on it and to a screen reader, and
the numbers beside the chart say the same in words.

## Arranging it

**Customise**, at the top right, turns the page into its layout. It is offered on a wide
window (1100 pixels or more): the grid has 12 columns there, 6 on a medium window and one
on a phone, where the widgets follow each other in reading order.

- **Move** a widget by dragging its title, **resize** it by dragging its bottom right
  corner. What is in the way moves down; nothing overlaps.
- **+ Widget** lists what you can add; the gear on a widget opens its options, the ×
  removes it.
- **Undo** and **Redo** (Ctrl+Z, Ctrl+Shift+Z) go through the changes of this visit.
- **Reset to default** brings back the layout a new account starts with.
- **Done** goes back to reading. Every change is saved as you make it.

From the keyboard alone: Tab reaches each widget; the arrows move it, Shift with the
arrows resizes it, Enter opens its options, Delete removes it. Each change is announced to
screen readers.

A page holds up to thirty widgets.

## The default layout

A new account starts with Clock, Shortcuts, Flow runs, Runs by day, Recent, Favourites,
Next runs, Failed runs and Note. The default is
the same on every installation, Docker Compose or Kubernetes; an administrator's has the
shortcuts to the administration too. Nothing is stored until you change something, so a
later version's better default reaches whoever kept the default.

## What it does not do

- A home page is not shared, and an administrator cannot set one for everybody.
- A widget cannot hold code or an address outside the installation: a shortcut is a page
  of Tabularia.
- A note is text: markup typed into it is shown, never run.

For a screen of numbers and charts that a team shares, build a
[dashboard](dashboards.md).
