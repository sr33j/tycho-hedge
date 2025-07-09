#!/bin/bash
set -e

# Initialize our own variables
CONTRACT_NAME=""
CONSTRUCTOR_SIGNATURE=""
CONSTRUCTOR_ARGUMENTS=""

# Function to display usage
usage() {
    echo "Usage: $0 -c contract_name [-s constructor_signature -a constructor_arguments]" 1>&2; exit 1;
}

while getopts ":c:s:a:" opt; do
    case "${opt}" in
        c)
            CONTRACT_NAME=${OPTARG};;
        s)
            CONSTRUCTOR_SIGNATURE=${OPTARG};;
        a)
            CONSTRUCTOR_ARGUMENTS=${OPTARG};;
        *)
            usage;;
    esac
done
shift $((OPTIND-1))

echo "CONTRACT_NAME: $CONTRACT_NAME"
echo "CONSTRUCTOR_SIGNATURE: $CONSTRUCTOR_SIGNATURE"
echo "CONSTRUCTOR_ARGUMENTS: $CONSTRUCTOR_ARGUMENTS"

# Perform operations if CONSTRUCTOR_SIGNATURE and CONSTRUCTOR_ARGUMENTS are set
if [[ ! -z "$CONSTRUCTOR_SIGNATURE" && ! -z "$CONSTRUCTOR_ARGUMENTS" ]]; then
    # Split the CONSTRUCTOR_ARGUMENTS by commas into an array
    IFS=',' read -r -a ARG_ARRAY <<< "$CONSTRUCTOR_ARGUMENTS"

    # Create the cast abi-encode command with the arguments
    ENCODED_ARGS=$(cast abi-encode "$CONSTRUCTOR_SIGNATURE" "${ARG_ARRAY[@]}")

    # Export the encoded arguments
    export __PROPELLER_DEPLOY_ARGS=$ENCODED_ARGS
    echo "$ENCODED_ARGS"
fi

export __PROPELLER_CONTRACT="$CONTRACT_NAME.sol:$CONTRACT_NAME"
export __PROPELLER_OUT_FILE="out/$CONTRACT_NAME.sol/$CONTRACT_NAME.evm.runtime"

forge script buildRuntime -v

echo "Write: $__PROPELLER_OUT_FILE"