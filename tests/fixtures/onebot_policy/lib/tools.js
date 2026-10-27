const defineTool = definition => definition;
export function registerTools(ctx, bridge, connection, limits) {
    const disposers = [];
    const register = (tool) => {
        disposers.push(ctx.tools.register(tool));
    };
    register(defineTool({
        name: 'qq_send_image',
        isConcurrencySafe: () => true,
        execute: async (args, exec) => {
            await ctx.waitMedia?.();
            const scope = args.chat_id || bridge.chatForSession(exec.agent.session.id);
            const messageId = await bridge.sendSegments(scope, [{ type: 'image', data: { file: 'fixture-only' } }]);
            return { sent: true, messageId };
        },
    }));
    register(defineTool({
        name: 'qq_send_forward',
        isConcurrencySafe: () => true,
        execute: async (args, exec) => bridge.sendForward(args.chat_id || bridge.chatForSession(exec.agent.session.id), args.messages),
    }));
    register(defineTool({
        name: 'qq_napcat_api',
        isConcurrencySafe: () => true,
        execute: (args) => connection.call(args.action, args.params || {}),
    }));
    return () => { for (const dispose of disposers) dispose(); };
}
