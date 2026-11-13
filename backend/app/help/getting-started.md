# Getting started

This assistant answers questions from the documents your organization gave it. Every answer lists the documents it used, and when the documents do not cover a question it says so instead of guessing.

## Signing in

Open the assistant's address in your browser.

- Username and password: type them on the sign-in screen. If your account has an authenticator app set up, you are asked for its six-digit code next.
- Continue as guest: only some instances allow this, and the button appears only on those. A guest can ask a limited number of questions per conversation (ten by default), gets shorter answers, and sees only the documents that are open to everyone.

There is no password-reset link. If you cannot sign in, ask your administrator. Once signed in, you can change your own username and password by clicking your name at the top of the page.

## Two-factor sign-in

You can add a second step to your own sign-in: a six-digit code from an authenticator app on your phone. Click your name at the top of the page, then the Two-factor tab. Type your password and click Start setup, scan the QR code with the app (or type the key shown under it), then type the app's six-digit code and click Turn on. From then on, signing in asks for a code after your password. Each code works once: the one you typed to turn two-factor on is used up, so your first sign-in takes the next code the app shows, and a code refused right after you used it somewhere else means the same - wait for the next one. There is no off switch on that tab.

## A new or lost authenticator

To move to a new phone or app, use Replace authenticator on the Two-factor tab. It asks for your password and a code from the authenticator you have now. Two-factor is off for your account from that moment until you confirm a code from the new one, so stay signed in until you have. Closing the screen before that asks you first.

If you lose the authenticator, ask your administrator to reset two-factor for your account, then set it up again. Where two-factor is required to sign in, an account that was reset cannot sign in until it is set up again: set it up from a session that is still signed in, or ask the person who runs the server to switch the requirement off while you do.

## Asking a question

Type in the box at the bottom and press Ctrl+Enter, or click the send button. Ask one thing at a time and be specific: "What is the notice period in the vendor onboarding policy?" works better than "tell me about policies". While an answer is streaming, the send button becomes Stop.

Follow-up questions in the same conversation keep the context of what was already said. To start over, click New Chat in the left panel. The menu button at the top left shows or hides the panel.

## Reading an answer

Under each answer you will see Sources: the documents the answer was built from. If an answer has no sources, treat it with more care: it was not grounded in a document.

Under an answer you can also click Copy, rate it with the thumbs up and thumbs down buttons when you are signed in (your administrator sees the ratings; an answer that was stopped or failed has none, because it was not saved), or click Regenerate for a second attempt at the same question. Edit, on your own message, lets you fix it without retyping; the answer is regenerated from the edited message.

## The left panel

- New Chat starts a fresh conversation.
- History lists your earlier conversations when you are signed in, grouped by when you last used them: Today, Last 7 days, Older. The pencil next to a conversation renames it (Enter saves, Escape cancels); the X deletes it, after asking once.
- A model list appears when your administrator lets people choose which AI model answers.
- Knowledge Base is a switch: on, answers are grounded in the documents; off, the assistant answers from general knowledge and says so. Your administrator can hide this switch, or set the instance to answer from documents only.

## The top bar

- Help is this guide. Click it, then ask how the assistant works in your own words. Click it again to go back to your documents.
- Trust opens a page of live measurements of how accurate and honest the assistant is on this instance.
- The badge next to Trust names the model that is answering.
- Admin appears for administrators. Sign in appears for visitors. Your name (account settings) and Sign out appear when you are signed in.
