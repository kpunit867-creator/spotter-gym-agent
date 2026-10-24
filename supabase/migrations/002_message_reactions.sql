-- Lets the bot react to a user's message with an emoji (WhatsApp "reaction" messages).
begin;

alter table messages drop constraint messages_content_type_check;
alter table messages add constraint messages_content_type_check
  check (content_type in ('text','image','audio','interactive','template','reaction'));

-- For reactions: the message being reacted to.
alter table messages add column reaction_to_message_id bigint
  references messages(message_id) on delete cascade;

commit;
