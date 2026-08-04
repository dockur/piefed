# Federation

## Supported federation protocols and standards

- [ActivityPub](https://www.w3.org/TR/activitypub/) (Server-to-Server)
- [WebFinger](https://webfinger.net/)
- [Http Signatures](https://datatracker.ietf.org/doc/html/draft-cavage-http-signatures)
- [NodeInfo](https://nodeinfo.diaspora.software/)

## Supported FEPS

- [FEP-67ff: FEDERATION.md](https://codeberg.org/fediverse/fep/src/branch/main/fep/67ff/fep-67ff.md)
- [FEP-f1d5: NodeInfo in Fediverse Software](https://codeberg.org/fediverse/fep/src/branch/main/fep/f1d5/fep-f1d5.md)
- [FEP-1b12: Group federation](https://codeberg.org/fediverse/fep/src/branch/main/fep/1b12/fep-1b12.md)
- [FEP-5feb: Search indexing consent for actors](https://codeberg.org/fediverse/fep/src/branch/main/fep/5feb/fep-5feb.md)
- [FEP-2677: Identifying the Application Actor](https://codeberg.org/fediverse/fep/src/branch/main/fep/2677/fep-2677.md)
- [FEP-3b86: Activity Intents](https://codeberg.org/fediverse/fep/src/branch/main/fep/3b86/fep-3b86.md)
- [FEP-7888: Context Property](https://codeberg.org/fediverse/fep/src/branch/main/fep/7888/fep-7888.md)
- [FEP-c0e0: Emoji reactions](https://codeberg.org/fediverse/fep/src/branch/main/fep/c0e0/fep-c0e0.md)
- [FEP-2345: fediverse:creator](https://codeberg.org/fediverse/fep/src/branch/main/fep/2345/fep-2345.md)

## Partially Supported FEPS

- [FEP-268d: Search consent signals for objects](https://codeberg.org/fediverse/fep/src/branch/main/fep/268d/fep-268d.md)
  - `"searchableBy": "https://www.w3.org/ns/activitystreams#Public"` == `"indexable": true`, any other content sets `"indexable": false`
  - `"searchableBy": ["https://alice.example/actor", "https://example.com/users/1/followers"]` is not currently supported.

## ActivityPub

- Communities are ['Group' actors](https://www.w3.org/TR/activitystreams-vocabulary/#dfn-group)
- Users are ['Person' actors](https://www.w3.org/TR/activitystreams-vocabulary/#dfn-person)
- Posts on Communities are one of the following:
  - [Page](https://www.w3.org/TR/activitystreams-vocabulary/#dfn-page)
  - [Article](https://www.w3.org/TR/activitystreams-vocabulary/#dfn-article)
  - [Link](https://www.w3.org/TR/activitystreams-vocabulary/#dfn-link)
  - [Note](https://www.w3.org/TR/activitystreams-vocabulary/#dfn-note)
  - [Question](https://www.w3.org/TR/activitystreams-vocabulary/#dfn-question)

## Additional documentation

See the `docs/activitypub_examples` for examples of [communities](./docs/activitypub_examples/communities.md), [users](./docs/activitypub_examples/users.md), and [posts](./docs/activitypub_examples/posts.md).