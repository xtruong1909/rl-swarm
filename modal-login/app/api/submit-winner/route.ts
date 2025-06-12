import { getLatestApiKey, getUser } from "@/app/db";
import { NextResponse } from "next/server";
import contract from "@/app/lib/contract.json";

import { encodeFunctionData, decodeErrorResult } from "viem";
import { LocalAccountSigner } from "@aa-sdk/core";
import {
  alchemy,
  createAlchemySmartAccountClient,
  gensynTestnet,
} from "@account-kit/infra";
import { createModularAccountV2 } from "@account-kit/smart-contracts";
import { SendUserOperationErrorType } from "viem/account-abstraction";
import { httpRequestErroDetailsStringSchema } from "@/app/lib/HttpRequestError";

export async function POST(request: Request) {
  const body: {
    orgId: string;
    roundNumber: bigint;
    winners: string[];
    peerId: string;
  } = await request.json().catch((err) => {
    console.error(err);
    console.log(body);
    return NextResponse.json(
      { error: "bad request generic" },
      {
        status: 400,
      },
    );
  });
  if (!body.orgId) {
    return NextResponse.json(
      { error: "bad request orgID" },
      {
        status: 400,
      },
    );
  }

  try {
    const user = getUser(body.orgId);
    if (!user) {
      return NextResponse.json(
        { error: "user not found" },
        {
          status: 404,
        },
      );
    }
    const apiKey = getLatestApiKey(body.orgId);
    if (!apiKey || !apiKey.deferredActionDigest || !apiKey.accountAddress) {
      return NextResponse.json(
        { error: "api key not found" },
        {
          status: 500,
        },
      );
    }
    const transport = alchemy({
      apiKey: process.env.NEXT_PUBLIC_ALCHEMY_API_KEY!,
    });

    const account = await createModularAccountV2({
      transport,
      chain: gensynTestnet,
      accountAddress: apiKey.accountAddress as `0x${string}`,
      signer: LocalAccountSigner.privateKeyToAccountSigner(
        apiKey.privateKey as `0x${string}`,
      ),
      deferredAction: apiKey.deferredActionDigest as `0x${string}`,
    });

    const client = createAlchemySmartAccountClient({
      account,
      chain: gensynTestnet,
      transport,
      policyId: process.env.NEXT_PUBLIC_PAYMASTER_POLICY_ID!,
    });

    const contractAdrr = process.env.SMART_CONTRACT_ADDRESS! as `0x${string}`;

    const { hash } = await client.sendUserOperation({
      uo: {
        target: contractAdrr,
        data: encodeFunctionData({
          abi: contract.abi,
          functionName: "submitWinners",
          args: [body.roundNumber, body.winners, body.peerId], // Your function arguments
        }),
      },
    });

    return NextResponse.json(
      {
        hash,
      },
      {
        status: 200,
      },
    );
  } catch (err) {
    console.error(err);
    // Casting is not ideal but is canonical way of handling errors as per the
    // viem docs.
    //
    // See: https://viem.sh/docs/error-handling#error-handling
    const error = err as SendUserOperationErrorType;
    if (error.name !== "HttpRequestError") {
      return NextResponse.json(
        {
          error: "An unexpected error occurred",
          original: error,
        },
        {
          status: 500,
        },
      );
    }

    const parsedDetailsResult = httpRequestErroDetailsStringSchema.safeParse(
      error.details,
    );

    if (!parsedDetailsResult.success) {
      return NextResponse.json(
        {
          error: "An unexpected error occurred getting request details",
          parseError: parsedDetailsResult.error,
          original: error.details,
        },
        {
          status: 500,
        },
      );
    }

    const {
      data: {
        data: { revertData },
      },
    } = parsedDetailsResult;

    const decodedError = decodeErrorResult({
      data: revertData,
      abi: contract.abi,
    });

    return NextResponse.json(
      {
        error: decodedError.errorName,
        metaMessages: error.metaMessages,
      },
      {
        status: 500,
      },
    );
  }
}
