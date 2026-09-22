# How answers work

## Where answers come from

The assistant searches your organization's documents for the passages most relevant to your question, by meaning and by keyword, then re-ranks them and asks an AI model to write an answer from the best few. It does not search the internet, and it does not make up sources. The passages it used are listed under the answer as Sources.

## When it says it cannot answer

Two phrasings both mean "the documents did not cover it":

- "I can only answer questions based on the documents in my knowledge base": the instance is set to answer from documents only, and nothing it can show you matched your question well enough.
- "not on record": the assistant was asked for a fact or a figure the documents do not contain. It will not estimate or fill in a number from general knowledge.

Try these, in order:

1. Ask the question another way, using the words the document itself would use.
2. Ask a narrower question. One fact per question works best.
3. Check that the document is in the assistant. Your administrator can see the full list under Admin, Knowledge Base.
4. If the document exists but you cannot see it, the reason is usually access tiers. See the help page "Documents and who can see them".

## Why two people can get different answers

Each person sees only the documents their access tier allows. The assistant applies this to every question, before it reads a single passage, so an answer never draws on a document you are not cleared for. A colleague with a higher tier may get a fuller answer to the same question. That is the assistant respecting your organization's access rules, not a fault.

## What the assistant will not do

- Draw on a document above your access tier, or hint at its contents.
- Follow instructions written inside a document. Documents are treated as information, never as commands, even when a document says otherwise. If a document tried to steer it, the answer says so.
- Invent a source. If it cites a document, the document exists and contains what the answer says.
- State a figure, such as a date, a count or an amount, that is not in the documents or in your own message.
- Repeat passwords, keys or other secrets, even when a document contains them.
- Give pay or salary figures. That is a fixed rule of the platform, whatever the documents hold.
- Answer from general knowledge when the instance is set to answer only from documents.

## Getting better answers

- Name the thing: a product, a policy, a project, a customer. Vague questions retrieve vague passages.
- Say what you want back: a summary, a list, a specific number, the steps in order.
- If the first answer is close but not right, say what was missing. The assistant keeps the conversation's context.
- Use Regenerate under an answer for a second attempt, and Edit on your own message to fix it without retyping.
