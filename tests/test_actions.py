from pytest import raises
from skribblpy import Phase, Client, PacketID, ActionError, DrawCommand


async def test_action_wire_payloads_and_authority(lobby):
    client = Client()
    client._store.apply(PacketID.LOBBY, lobby)
    sent = []

    async def send(packet_id, data=None):
        sent.append((packet_id, data))

    client._send = send
    with raises(ActionError):
        await client.kick(2)
    await client.vote_kick(2)
    await client.report(2, 5)
    await client.toggle_mute(2)
    await client.vote_drawing(True)
    assert sent == [(5, 2), (6, {'id': 2, 'reasons': 5}), (7, 2), (8, 1)]
    client._store.apply(PacketID.OWNER, 1)
    await client.set_setting(2, 80)
    await client.start_game(['cat', 'dog', 'sun', 'tree', 'bird'])
    with raises(ValueError):
        await client.start_game(['cat'])
    with raises(ValueError):
        await client.report(2, 8)
    client._store.apply(PacketID.STATE, {'id': Phase.CHOOSING, 'data': {'words': ['cat', 'dog']}})
    await client.choose_word(1)
    assert sent[-1] == (18, 1)
    with raises(ValueError):
        await client.choose_word(2)
    client._store.apply(PacketID.STATE, {'id': Phase.DRAWING, 'data': {'id': 1, 'word': 'dog'}})
    commands = [DrawCommand.fill(4)] * 9
    await client.send_drawing(commands)
    assert len(sent[-2][1]) == 8 and len(sent[-1][1]) == 1
    await client.undo_drawing(3)
    await client.clear_drawing()
    assert sent[-2:] == [(21, 3), (20, None)]
