// Open/close flag for the Send Solari dialog. Needed because the trigger button
// lives inside ChoamBankCard (deep in the Storage page tree, under panels that
// translateY-animate on enter -- a transform ancestor breaks position:fixed
// centering) while the dialog itself has to mount at app-shell level, same as
// AccountManageDialog does in +layout.svelte. A single $state proxy is the
// established way this app bridges that gap (see mailbox.svelte.js).
export const sendSolari = $state({ open: false });

export function openSendSolari() {
  sendSolari.open = true;
}

export function closeSendSolari() {
  sendSolari.open = false;
}
