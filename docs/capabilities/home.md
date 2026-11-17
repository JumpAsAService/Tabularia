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

## The widgets

| Widget | What it shows | Its options |
|---|---|---|
| **Clock** | The time and the date, with the time zone | The zone (the browser's, or one you choose), 12 or 24 hours, the seconds |
| **Recent** | The objects you changed last, of any kind, with their folder | How many (5 to 20), and which kinds |
| **Failed runs** | The last runs that failed, each with the first line of its error and a link to it | Yours alone or all those you may see; how many days back (1 to 30) |
| **Shortcuts** | Buttons to the pages you use most | Which pages |
| **Note** | A text of your own: a reminder, a link | The text, with headings, lists, bold and code as in Markdown |

A widget reads the same data as the page it mirrors, with your rights: *Failed runs*
lists what the **Runs** page would list for you, *Recent* what Explore would. Putting a
widget on the page never shows more than you may already see. *Recent* and *Failed runs*
read again every minute while the page is in view.

More widgets come with the next pieces of the feature: the objects you pin, the runs due
next, the duration of your flows, and for administrators the state of the installation.

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

A new account starts with Clock, Shortcuts, Recent, Failed runs and Note. The default is
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
