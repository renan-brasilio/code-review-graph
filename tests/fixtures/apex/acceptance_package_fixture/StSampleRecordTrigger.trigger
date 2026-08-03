trigger StSampleRecordTrigger on Sample_Record__c (after insert) {
    sitetracker.StTriggerFactory.createAndExecuteHandler(StSampleRecordTriggerHandler.class);
}
