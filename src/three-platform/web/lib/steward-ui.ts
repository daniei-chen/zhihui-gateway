import type {ConversationMessage} from './steward';

export function recipientProblem(value: string, existing: string[]): 'invalid' | 'duplicate' | 'limit' | null {
  const address = value.trim();
  if (address.length > 254 || !/^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}$/.test(address)
      || address.includes('..') || address.startsWith('.') || address.includes('.@')) return 'invalid';
  if (existing.some(item => item.toLowerCase() === address.toLowerCase())) return 'duplicate';
  if (existing.length >= 10) return 'limit';
  return null;
}

export function normalizeRecipient(value: string): string {
  const [local, domain] = value.trim().split('@');
  return `${local}@${domain.toLowerCase()}`;
}

export function conversationHistory(messages: ConversationMessage[]): ConversationMessage[] {
  return messages.slice(-8).map(message => ({role: message.role,
    content: message.content.slice(0, message.role === 'assistant' ? 6000 : 2000)}));
}
