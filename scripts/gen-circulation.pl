# Generate circulation history in a koha-testing-docker instance.
#
# KTD ships no loans, so the collaborative layer has nothing to learn
# from. This produces history with the shape real circulation has:
#
#   * Zipf-ish title popularity -- a few books account for most loans,
#     which is the skew collaborative filtering actually exploits.
#   * Skewed patron activity -- a few heavy borrowers, a long tail of
#     people who took out one or two books.
#   * Renewals and occasional repeat borrows, so the confidence formula
#     has terms to weigh rather than a single flat signal.
#
# It is still synthetic. It exercises the code paths; it says nothing
# about whether the recommendations are any good.
#
# AddIssue reads C4::Context->userenv->{branch}, which is undef outside
# a web session. t::lib::Mocks::mock_userenv is how Koha's own test
# suite establishes it.
use strict;
use warnings;
use lib '/kohadevbox/koha/t/lib';
use lib '/kohadevbox/koha';

use C4::Context;
use C4::Circulation qw( AddIssue AddReturn AddRenewal );
use Koha::Items;
use Koha::Patrons;
use Koha::Libraries;
use t::lib::Mocks;

my $lib   = Koha::Libraries->search({}, { rows => 1 })->next;
my $staff = Koha::Patrons->search({}, { rows => 1 })->next;
t::lib::Mocks::mock_userenv({ patron => $staff, branchcode => $lib->branchcode });

my @patrons;
my $ps = Koha::Patrons->search({}, { rows => 50 });
while (my $p = $ps->next) { push @patrons, $p }

my @items;
my $is = Koha::Items->search({ onloan => undef, notforloan => 0 }, { rows => 900 });
while (my $i = $is->next) { push @items, $i if $i->barcode }

printf "    %d patrons, %d lendable items\n", scalar(@patrons), scalar(@items);

srand(42);   # reproducible

my @weighted;
for my $rank (0 .. $#items) {
    push @weighted, $rank for 1 .. int(60 / ($rank + 1)) + 1;
}
my @activity = map { int(40 / ($_ + 1)) + 1 } 0 .. $#patrons;

my ($issued, $renewed, $repeats, $failed) = (0, 0, 0, 0);

for my $pi (0 .. $#patrons) {
    my $patron = $patrons[$pi];
    my %already;

    for (1 .. $activity[$pi]) {
        my $item = $items[ $weighted[ int(rand(scalar @weighted)) ] ] or next;
        my $issue = eval {
            AddIssue($patron, $item->barcode, undef, undef, undef, undef, {})
        };
        unless ($issue) { $failed++; next }
        $issued++;

        if (rand() < 0.25) {
            eval { AddRenewal({ borrowernumber => $patron->borrowernumber,
                                itemnumber     => $item->itemnumber,
                                branch         => $lib->branchcode }) };
            $renewed++;
        }

        if (rand() < 0.8) {
            eval { AddReturn($item->barcode, $lib->branchcode) };

            if (rand() < 0.08 && !$already{ $item->itemnumber }) {
                my $again = eval {
                    AddIssue($patron, $item->barcode, undef, undef, undef, undef, {})
                };
                if ($again) {
                    $repeats++; $issued++;
                    eval { AddReturn($item->barcode, $lib->branchcode) };
                }
            }
        }
        $already{ $item->itemnumber } = 1;
    }
}

printf "    issued=%d renewed=%d repeats=%d failed=%d\n",
       $issued, $renewed, $repeats, $failed;
