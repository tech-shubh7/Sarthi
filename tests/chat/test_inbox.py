import asyncio

from sarthi.chat.inbox import Inbox, Message


def text(value: str) -> Message:
    return {"text": value}


async def settle() -> None:
    await asyncio.sleep(0.15)


async def test_a_quick_burst_is_handled_as_one_batch() -> None:
    batches: list[list[str]] = []

    async def handle(chat_id: int, batch: list[Message]) -> None:
        batches.append([m["text"] for m in batch])

    async def notice(chat_id: int) -> None:
        raise AssertionError("no notice expected")

    inbox = Inbox(handle, notice, quiet_seconds=0.05)
    await inbox.put(1, text("remove fintech"))
    await inbox.put(1, text("and add ML engineer"))
    await settle()
    assert batches == [["remove fintech", "and add ML engineer"]]
    await inbox.close()


async def test_messages_during_a_batch_wait_and_get_one_notice() -> None:
    batches: list[list[str]] = []
    notices: list[int] = []
    release = asyncio.Event()

    async def slow(chat_id: int, batch: list[Message]) -> None:
        batches.append([m["text"] for m in batch])
        if len(batches) == 1:
            await release.wait()  # e.g. the AI is reading the answer

    async def notice(chat_id: int) -> None:
        notices.append(chat_id)

    inbox = Inbox(slow, notice, quiet_seconds=0.02)
    await inbox.put(7, text("first"))
    await asyncio.sleep(0.05)
    assert inbox.busy
    for value in ("a", "b", "c"):
        await inbox.put(7, text(value))  # flood while busy
    release.set()
    await settle()
    assert batches == [["first"], ["a", "b", "c"]]
    assert notices == [7]  # one notice, not three
    assert not inbox.busy
    await inbox.close()


async def test_a_failing_batch_does_not_stop_the_chat() -> None:
    seen: list[str] = []

    async def handle(chat_id: int, batch: list[Message]) -> None:
        seen.append(batch[0]["text"])
        if batch[0]["text"] == "boom":
            raise RuntimeError("bug")

    async def notice(chat_id: int) -> None:
        return None

    inbox = Inbox(handle, notice, quiet_seconds=0.01)
    await inbox.put(1, text("boom"))
    await settle()
    await inbox.put(1, text("next"))
    await settle()
    assert seen == ["boom", "next"]
    await inbox.close()
