import { UnifiedApiClient } from "./unified-api-client";

async function run() {
  const client = new UnifiedApiClient({
    cacheEnabled: true,
    defaultCacheTtlMs: 60000
  });

  console.log("---- REST Example ----");

  const rest = await client.request({
    kind: "rest",
    url: "https://jsonplaceholder.typicode.com/users",
    method: "GET",
    cache: { enabled: true, ttlMs: 20000 }
  });

  if (rest.ok) {
    console.log("Users:", rest.data.slice(0, 2));
  } else {
    console.error("Error:", rest.error);
  }

  console.log("---- GraphQL Example ----");

  const gql = await client.request({
    kind: "graphql",
    url: "https://countries.trevorblades.com/",
    query: `
      query {
        country(code: "IN") {
          name
          capital
          currency
        }
      }
    `,
    cache: { enabled: true, ttlMs: 20000 }
  });

  if (gql.ok) {
    console.log("Country:", gql.data);
  } else {
    console.error("GraphQL Error:", gql.error);
  }
}

run();