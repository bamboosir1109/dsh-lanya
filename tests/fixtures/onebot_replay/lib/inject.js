export const INTERCEPTED_MESSAGE_ID = -1;
export const isWriteAction = action => action.startsWith('send_') || action.startsWith('set_') || action === 'delete_msg';
