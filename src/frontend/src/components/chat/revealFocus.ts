/**
 * Where the focus goes after a "+N" control revealed more items and removed
 * itself: to the first revealed item -- its first link or button, or, for an
 * item that opens nothing, the item itself. Without this the focus falls to
 * <body> and a keyboard user starts over at the top of the page.
 *
 * An item without a control gets `tabIndex = -1`: focusable by script, NOT
 * part of the tab order, so a dead item still takes no Tab stop.
 */
export function focusRevealedItem(item: Element | null | undefined): void {
  if (!(item instanceof HTMLElement)) return;
  const control = item.matches('a[href], button')
    ? item
    : item.querySelector<HTMLElement>('a[href], button');
  if (control) {
    control.focus();
    return;
  }
  item.tabIndex = -1;
  item.focus();
}
