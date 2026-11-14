// How a paired instance is named everywhere a peer is shown.
//
// A pairing has two names: the far INSTANCE's own name (sent by that instance
// in a verified attachment, or -- until one arrives -- the host of its stored
// endpoint) and the PERSON who paired. The instance is primary, the person
// secondary: "Instance B · alex". Without an instance label the person stands
// alone, exactly as before the instance name existed.
//
// Both are DISPLAY text from the far side: never a key, never a lookup value.

export interface PeerLabelParts {
  /** What the peer is called: the instance when known, else the person. */
  primary: string;
  /** The person who paired -- only when `primary` is the instance. */
  secondary?: string;
}

export const PEER_LABEL_SEPARATOR = ' · ';

export function peerLabelParts(
  instanceName: string | null | undefined,
  personName: string | null | undefined,
): PeerLabelParts {
  const instance = (instanceName ?? '').trim();
  const person = (personName ?? '').trim();
  if (!instance) return { primary: person };
  // Saying the same word twice ("alex · alex") tells nobody anything.
  if (!person || person === instance) return { primary: instance };
  return { primary: instance, secondary: person };
}

/** One line: "instance · person", or the one name there is. */
export function formatPeerLabel(
  instanceName: string | null | undefined,
  personName: string | null | undefined,
): string {
  const { primary, secondary } = peerLabelParts(instanceName, personName);
  return secondary ? `${primary}${PEER_LABEL_SEPARATOR}${secondary}` : primary;
}
